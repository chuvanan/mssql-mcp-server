"""T-SQL lexing and statement classification.

This module decides whether model-supplied SQL only reads. It imports nothing
else from the package so that its tests stay pure: no env, no mocks, no async.

The classifier is a *guardrail, not a security boundary*. A determined payload
can still reach the database through a linked server or an extended procedure.
The real controls are, in order:

1. a read-only SQL login (see SECURITY.md);
2. the read path in ``db.py``, which always runs with ``autocommit=False`` and
   always rolls back -- in SQL Server, unlike MySQL, DDL is transactional;
3. the ``cursor.description is None`` tripwire in ``db.py``, which turns any
   hole in this module into a loud logged event rather than a silent write;
4. this classifier.

The lexer exists because naive string matching cannot tell a comment from a
comment-shaped string literal. ``SELECT '--'``, ``SELECT ';'``,
``SELECT [drop--table]`` and ``SELECT N'/*'`` all defeat a regex approach.
Literal and quoted-identifier *contents* are discarded during lexing, which is
what makes those cases fall out for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, StrEnum

__all__ = [
    "SqlLexError",
    "Statement",
    "StatementKind",
    "Token",
    "TokenKind",
    "classify",
    "is_read_only",
    "needs_autocommit",
    "parse",
    "split_statements",
    "validate_table_name",
]


class SqlLexError(ValueError):
    """The SQL could not be lexed (unterminated string, identifier or comment).

    Always treated as a write. Failing closed is the point: if we cannot read
    the statement, we must not assume it is harmless.
    """


class TokenKind(Enum):
    WORD = "word"
    NUMBER = "number"
    LITERAL = "literal"
    QUOTED_ID = "quoted_id"
    PUNCT = "punct"
    SEMI = "semi"


class StatementKind(StrEnum):
    """What a single statement does, at the granularity the user needs to see."""

    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MERGE = "MERGE"
    DDL = "DDL"
    EXEC = "EXEC"
    TRANSACTION = "TRANSACTION"
    PERMISSION = "PERMISSION"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str  # upper-cased for WORD; empty for LITERAL/QUOTED_ID/NUMBER
    depth: int  # parenthesis nesting depth
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Statement:
    text: str
    tokens: tuple[Token, ...]
    kind: StatementKind

    @property
    def top_level_words(self) -> tuple[str, ...]:
        return tuple(t.text for t in self.tokens if t.kind is TokenKind.WORD and t.depth == 0)


# Statement-introducing keywords, used to resolve what a CTE actually feeds.
_STATEMENT_HEADS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"})

# Any of these appearing at depth 0 disqualifies a statement from being a read.
# Deliberately broad: a false "this is a write" costs one clear error message,
# a false "this is a read" costs data.
_WRITE_TOKENS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "EXEC",
        "EXECUTE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "RENAME",
        "GRANT",
        "REVOKE",
        "DENY",
        "BACKUP",
        "RESTORE",
        "DBCC",
        "KILL",
        "SHUTDOWN",
        "RECONFIGURE",
        "COMMIT",
        "ROLLBACK",
        "SAVE",
        "BEGIN",
        "TRAN",
        "TRANSACTION",
        "USE",
        "SET",
        "DECLARE",
        "WAITFOR",
        "BULK",
        # Passthrough surfaces: these can carry a write inside a string that we
        # deliberately never look inside.
        "OPENQUERY",
        "OPENROWSET",
        "OPENDATASOURCE",
    }
)

_DDL_HEADS = frozenset(
    {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "BACKUP", "RESTORE", "DBCC"}
)
_PERMISSION_HEADS = frozenset({"GRANT", "REVOKE", "DENY"})
_TRANSACTION_HEADS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVE"})

# Statements SQL Server refuses to run inside an explicit transaction.
_AUTOCOMMIT_ONLY: frozenset[tuple[str, ...]] = frozenset(
    {
        ("CREATE", "DATABASE"),
        ("ALTER", "DATABASE"),
        ("DROP", "DATABASE"),
        ("CREATE", "FULLTEXT"),
        ("ALTER", "FULLTEXT"),
        ("DROP", "FULLTEXT"),
        ("BACKUP",),
        ("RESTORE",),
        ("RECONFIGURE",),
    }
)

_WHITESPACE = " \t\r\n\f\v﻿"
_WORD_START = re.compile(r"[A-Za-z_@#]")
_WORD_BODY = re.compile(r"[A-Za-z0-9_@#$]")
_TABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)?$")


def _lex(sql: str) -> list[Token]:
    """Tokenize T-SQL. Raises SqlLexError on anything it cannot close."""
    tokens: list[Token] = []
    i, n, depth = 0, len(sql), 0

    while i < n:
        ch = sql[i]

        if ch in _WHITESPACE:
            i += 1
            continue

        # -- line comment (dropped)
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue

        # /* block comment */ -- T-SQL nests these, so count depth rather than
        # using a non-greedy regex, which would stop at the first */.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            start = i
            comment_depth = 0
            while i < n:
                if sql.startswith("/*", i):
                    comment_depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    comment_depth -= 1
                    i += 2
                    if comment_depth == 0:
                        break
                else:
                    i += 1
            if comment_depth != 0:
                raise SqlLexError(f"Unterminated block comment starting at offset {start}.")
            continue

        # 'string literal', optionally with a unicode N prefix. Contents are
        # discarded -- this is the defense against comment- and
        # separator-shaped literals.
        if ch == "'" or (ch in "Nn" and i + 1 < n and sql[i + 1] == "'"):
            start = i
            if ch in "Nn":
                i += 1
            i += 1  # past the opening quote
            while True:
                if i >= n:
                    raise SqlLexError(f"Unterminated string literal starting at offset {start}.")
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # '' escape
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(Token(TokenKind.LITERAL, "", depth, start, i))
            continue

        # "quoted identifier" (QUOTED_IDENTIFIER is ON by default)
        if ch == '"':
            start = i
            i += 1
            while True:
                if i >= n:
                    raise SqlLexError(f"Unterminated quoted identifier at offset {start}.")
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(Token(TokenKind.QUOTED_ID, "", depth, start, i))
            continue

        # [bracket identifier]
        if ch == "[":
            start = i
            i += 1
            while True:
                if i >= n:
                    raise SqlLexError(f"Unterminated bracketed identifier at offset {start}.")
                if sql[i] == "]":
                    if i + 1 < n and sql[i + 1] == "]":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(Token(TokenKind.QUOTED_ID, "", depth, start, i))
            continue

        if _WORD_START.match(ch):
            start = i
            i += 1
            while i < n and _WORD_BODY.match(sql[i]):
                i += 1
            tokens.append(Token(TokenKind.WORD, sql[start:i].upper(), depth, start, i))
            continue

        if ch.isdigit():
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] == "."):
                i += 1
            tokens.append(Token(TokenKind.NUMBER, "", depth, start, i))
            continue

        if ch == "(":
            tokens.append(Token(TokenKind.PUNCT, "(", depth, i, i + 1))
            depth += 1
            i += 1
            continue

        if ch == ")":
            depth = max(0, depth - 1)
            tokens.append(Token(TokenKind.PUNCT, ")", depth, i, i + 1))
            i += 1
            continue

        if ch == ";" and depth == 0:
            tokens.append(Token(TokenKind.SEMI, ";", depth, i, i + 1))
            i += 1
            continue

        tokens.append(Token(TokenKind.PUNCT, ch, depth, i, i + 1))
        i += 1

    return tokens


def _classify_tokens(tokens: tuple[Token, ...]) -> StatementKind:
    words = [t.text for t in tokens if t.kind is TokenKind.WORD and t.depth == 0]
    if not words:
        return StatementKind.OTHER

    head = words[0]

    # A CTE is named for what it feeds: WITH x AS (...) DELETE is a DELETE.
    if head == "WITH":
        head = next((w for w in words[1:] if w in _STATEMENT_HEADS), "WITH")

    if head == "SELECT":
        # SELECT ... INTO creates and populates a table. It is a write.
        return StatementKind.INSERT if "INTO" in words else StatementKind.SELECT
    if head in ("INSERT", "UPDATE", "DELETE", "MERGE"):
        return StatementKind[head]
    if head in ("EXEC", "EXECUTE"):
        return StatementKind.EXEC
    if head in _DDL_HEADS:
        return StatementKind.DDL
    if head in _PERMISSION_HEADS:
        return StatementKind.PERMISSION
    if head in _TRANSACTION_HEADS:
        return StatementKind.TRANSACTION
    return StatementKind.OTHER


def parse(sql: str) -> list[Statement]:
    """Split ``sql`` into classified statements.

    Raises:
        SqlLexError: if the SQL cannot be lexed, or contains a ``GO`` batch
            separator (a sqlcmd client directive, not T-SQL -- passing it to
            ODBC produces a baffling syntax error).
    """
    tokens = _lex(sql)

    if any(t.kind is TokenKind.WORD and t.text == "GO" and t.depth == 0 for t in tokens):
        raise SqlLexError(
            "'GO' is a sqlcmd batch separator, not a T-SQL statement, and cannot be sent "
            "to the server. Submit each batch as a separate call."
        )

    statements: list[Statement] = []
    current: list[Token] = []

    def flush() -> None:
        if not current:
            return
        text = sql[current[0].start : current[-1].end]
        frozen = tuple(current)
        statements.append(Statement(text, frozen, _classify_tokens(frozen)))
        current.clear()

    for token in tokens:
        if token.kind is TokenKind.SEMI:
            flush()
        else:
            current.append(token)
    flush()

    return statements


def split_statements(sql: str) -> list[str]:
    """Return the source text of each statement. Raises SqlLexError."""
    return [s.text for s in parse(sql)]


def classify(sql: str) -> list[StatementKind]:
    """Classify each statement. Never raises: a lex failure yields ``[UNKNOWN]``."""
    try:
        return [s.kind for s in parse(sql)] or [StatementKind.UNKNOWN]
    except SqlLexError:
        return [StatementKind.UNKNOWN]


def is_read_only(sql: str) -> bool:
    """True only if ``sql`` is a single, unambiguously read-only statement.

    This is an allowlist. Everything that is not provably a bare SELECT --
    including anything that fails to lex -- is treated as a write.
    """
    try:
        statements = parse(sql)
    except SqlLexError:
        return False

    if len(statements) != 1:
        return False

    statement = statements[0]
    words = statement.top_level_words
    if not words:
        return False

    head = words[0]
    if head == "WITH":
        head = next((w for w in words[1:] if w in _STATEMENT_HEADS), "")
    if head != "SELECT":
        return False

    # SELECT ... INTO is a write wearing a SELECT costume.
    if "INTO" in words:
        return False

    # Belt and braces: rule 3 should already have caught these.
    return not any(w in _WRITE_TOKENS for w in words)


def needs_autocommit(sql: str) -> bool:
    """True if any statement cannot run inside an explicit transaction."""
    try:
        statements = parse(sql)
    except SqlLexError:
        return False

    for statement in statements:
        words = statement.top_level_words
        for prefix in _AUTOCOMMIT_ONLY:
            if tuple(words[: len(prefix)]) == prefix:
                return True
    return False


def validate_table_name(table_name: str) -> str:
    """Validate a ``table`` or ``schema.table`` name and return it bracketed.

    The only thing standing between a URI path segment and interpolated SQL,
    so the allowlist is deliberately strict: it rejects ``$``, ``#`` and
    non-ASCII identifiers that SQL Server would otherwise accept.
    """
    if not _TABLE_NAME_RE.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    # The regex already forbids ']', but escape anyway so this stays correct
    # if the pattern is ever loosened.
    return ".".join(f"[{part.replace(']', ']]')}]" for part in table_name.split("."))
