"""The write-approval checkpoint.

Before any statement that modifies data or schema, the user is shown the exact
SQL and asked to approve it, via MCP elicitation.

The design is fail-closed. ``ctx.elicit()`` raises when the connected client
does not implement elicitation, and that would surface to the model as an
opaque protocol error, so the capability is checked *first* and a missing
capability refuses the write with an actionable message. Both the check and the
elicit call are wrapped, because the only acceptable failure mode here is
"did not run the statement".

No database connection is opened until the decision is APPROVED, which is why
``test_security.py`` can assert that a declined write never reached the driver.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

import mcp.types as mcp_types

from mssql_mcp_server.config import ApprovalMode, Settings
from mssql_mcp_server.sql import StatementKind

logger = logging.getLogger("mssql_mcp_server.approval")

__all__ = ["Decision", "APPROVE_OPTION", "REJECT_OPTION", "build_message", "request_approval"]

APPROVE_OPTION = "Approve - run this statement"
REJECT_OPTION = "Reject - do not run"

# Long statements are truncated in the prompt, but never silently: a user who
# cannot see what they are approving is not really approving it.
_MAX_SQL_IN_PROMPT = 4000


class Decision(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    """The client cannot show an approval prompt."""
    FORBIDDEN = "forbidden"
    """Writes are disabled by configuration."""


def build_message(sql: str, kinds: list[StatementKind], settings: Settings) -> str:
    """The text shown to the user in the approval prompt.

    Leads with server and database, because "which database am I about to
    modify" is the question that actually matters.
    """
    if len(sql) > _MAX_SQL_IN_PROMPT:
        shown = (
            f"{sql[:_MAX_SQL_IN_PROMPT]}\n"
            f"... [showing the first {_MAX_SQL_IN_PROMPT} of {len(sql)} characters]"
        )
    else:
        shown = sql

    unique_kinds = list(dict.fromkeys(str(k) for k in kinds))

    return (
        "Approve this write against SQL Server?\n\n"
        f"  Server:     {settings.server}\n"
        f"  Database:   {settings.database}\n"
        f"  Statements: {', '.join(unique_kinds)}\n\n"
        f"{shown}\n\n"
        "This will modify data or schema. Rejecting is safe: nothing has been "
        "executed yet and no database connection has been opened."
    )


def _client_supports_elicitation(ctx: Any) -> bool:
    """Whether the connected client can show a prompt at all.

    Wrapped broadly: some transports have no session, and a missing capability
    must read as "cannot ask", never as a crash.
    """
    try:
        return bool(
            ctx.session.check_client_capability(
                mcp_types.ClientCapabilities(elicitation=mcp_types.ElicitationCapability())
            )
        )
    except Exception:
        logger.debug("Could not determine client elicitation capability.", exc_info=True)
        return False


async def request_approval(
    ctx: Any,
    *,
    sql: str,
    kinds: list[StatementKind],
    settings: Settings,
) -> Decision:
    """Ask the user to approve ``sql``. Never raises."""
    if settings.approval_mode is ApprovalMode.READONLY:
        return Decision.FORBIDDEN

    if settings.approval_mode is ApprovalMode.ALLOW:
        # Loud on every write, so that a mode set once in a config file does
        # not quietly stay invisible for the life of the session.
        logger.warning("MSSQL_APPROVAL_MODE=allow: executing a write without user review.")
        await _warn(ctx, "Approval checkpoint is disabled (MSSQL_APPROVAL_MODE=allow).")
        return Decision.APPROVED

    if not _client_supports_elicitation(ctx):
        return Decision.UNAVAILABLE

    try:
        result = await ctx.elicit(
            build_message(sql, kinds, settings),
            response_type=[APPROVE_OPTION, REJECT_OPTION],
        )
    except Exception:
        # A false positive from the capability check still must not write.
        logger.warning("Elicitation failed; refusing the write.", exc_info=True)
        return Decision.UNAVAILABLE

    action = getattr(result, "action", None)
    if action == "accept":
        # data is populated only on accept.
        return (
            Decision.APPROVED
            if getattr(result, "data", None) == APPROVE_OPTION
            else Decision.DECLINED
        )
    if action == "decline":
        return Decision.DECLINED
    return Decision.CANCELLED


async def _warn(ctx: Any, message: str) -> None:
    try:
        await ctx.warning(message)
    except Exception:
        logger.debug("Could not send a warning to the client.", exc_info=True)
