# Windows note: run these under Git Bash, or use the underlying commands
# directly. `make` is not present in a stock PowerShell.
.PHONY: help install db-up db-down db-reset migrate downgrade revision check \
        lint format typecheck test test-fast verify clean

help:
	@echo "install    - create .venv and install the project with dev extras"
	@echo "db-up      - start Postgres 16 on localhost:5433"
	@echo "db-down    - stop Postgres (keeps the volume)"
	@echo "db-reset   - DROP and recreate the dev database, then migrate"
	@echo "migrate    - alembic upgrade head"
	@echo "revision   - autogenerate a migration (m='message')"
	@echo "check      - alembic check: models must match migrations"
	@echo "lint       - ruff check + format check"
	@echo "format     - ruff format (writes)"
	@echo "typecheck  - mypy"
	@echo "test       - full suite (needs Postgres)"
	@echo "test-fast  - pure-logic tests only, no database, no bulk fixtures"
	@echo "verify     - everything CI runs"
	@echo ""
	@echo "capture    - one raw slate capture, no database (2 credits)"
	@echo "schedule   - pull yesterday..tomorrow into games"
	@echo "poll       - capture + parse + store snapshots (2 credits)"
	@echo "replay     - re-parse every raw/ capture; spends nothing"
	@echo "status     - ingest health, credits, close-proximity"
	@echo "clv        - settled bets, coverage, realised P&L"

PY := .venv/Scripts/python.exe

install:
	py -3.13 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

db-up:
	docker compose up -d postgres

db-down:
	docker compose stop postgres

# Recreating rather than TRUNCATE-ing: odds_snapshots is append-only by
# policy, so "delete the rows" is not a thing this project does to itself.
db-reset:
	docker exec baseballv2-postgres-1 psql -U baseball -d postgres \
		-c "DROP DATABASE IF EXISTS baseball WITH (FORCE);" \
		-c "CREATE DATABASE baseball;"
	$(PY) -m alembic upgrade head

migrate:
	$(PY) -m alembic upgrade head

downgrade:
	$(PY) -m alembic downgrade base

revision:
	$(PY) -m alembic revision --autogenerate -m "$(m)"

check:
	$(PY) -m alembic check

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest -q

# The inner loop: no Docker required. Everything in betting/ except the
# query layer is pure, which is deliberate.
test-fast:
	$(PY) -m pytest -q -m "not postgres and not performance"

verify: lint typecheck check test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +

# --- ingest ---------------------------------------------------------------

# Standalone, stdlib only, no database. For capturing a slate before the
# rest of the pipeline is ready to store it.
capture:
	$(PY) scripts/dump_odds.py

schedule:
	$(PY) -m ingestion schedule

poll:
	$(PY) -m ingestion poll

# Costs nothing: re-parses captures already on disk. This is the recovery
# path for a parser bug, which is why the poller writes raw before parsing.
replay:
	$(PY) -m ingestion.replay

# --- reporting ------------------------------------------------------------
# psql inside the container, so no local client is needed. The only
# interface for now: no dashboard.

status:
	@docker exec -i baseballv2-postgres-1 psql -U baseball -d baseball -q 		-v ON_ERROR_STOP=1 -f - < sql/status.sql

clv:
	@docker exec -i baseballv2-postgres-1 psql -U baseball -d baseball -q 		-v ON_ERROR_STOP=1 -f - < sql/clv.sql
