.PHONY: install install-dev test test-live lint format clean run inspect dev \
        docker-build docker-up docker-down docker-test docker-exec check-connection

UV := uv

install:
	$(UV) sync --no-dev

install-dev:
	$(UV) sync --group dev

test: install-dev
	$(UV) run pytest -v

# Requires a live SQL Server; see docker-up.
test-live: install-dev
	MSSQL_LIVE_TESTS=1 $(UV) run pytest -m live -v

lint: install-dev
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts
	$(UV) run mypy src --ignore-missing-imports

format: install-dev
	$(UV) run ruff format src tests scripts
	$(UV) run ruff check --fix src tests scripts

clean:
	rm -rf .venv __pycache__ .pytest_cache .coverage .ruff_cache .mypy_cache dist
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

run: install
	$(UV) run python -m mssql_mcp_server

# Print the tool surface without connecting to anything.
inspect: install-dev
	$(UV) run fastmcp inspect src/mssql_mcp_server/server.py:mcp

# Launch the MCP Inspector, which can render the approval prompt.
dev: install-dev
	$(UV) run fastmcp dev src/mssql_mcp_server/server.py:mcp

# Docker
docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-test:
	docker compose exec mcp_server pytest -v

docker-exec:
	docker compose exec mcp_server bash

# Diagnose the connection using the same configuration the server uses.
# Export MSSQL_* first; the script takes no arguments.
check-connection:
	$(UV) run python scripts/check_connection.py
