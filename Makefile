# Derp — developer commands. Run `make help` for the list.
.DEFAULT_GOAL := help
UV := uv run --no-sync

TEST_DB_PORT ?= 5433
TEST_DATABASE_URL ?= postgresql+asyncpg://derp:derp@localhost:$(TEST_DB_PORT)/derp_test
DEV_DB_PORT ?= 5434
DEV_DATABASE_URL ?= postgresql+asyncpg://derp:derp@localhost:$(DEV_DB_PORT)/derp

.PHONY: help install lint format typecheck check test test-db test-e2e test-all db-test-up db-test-down db-dev-up migrate revision run preflight import-main

help: ## List targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Sync dependencies (incl. dev)
	uv sync

lint: ## Ruff lint + format check
	$(UV) ruff check .
	$(UV) ruff format --check .

format: ## Apply Ruff fixes and formatting
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck: ## Pyright (strict on money/history/agent)
	$(UV) pyright

check: lint typecheck test-all ## Everything CI runs

test: ## Unit tests (no database)
	$(UV) pytest -m "not db and not e2e and not live" -q

test-db: db-test-up ## PostgreSQL tests
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) pytest -m "db and not e2e" -q

test-e2e: db-test-up ## Fake Bot API journeys
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) pytest -m e2e -q

test-all: db-test-up ## All offline tests
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) pytest -q

db-test-up: ## Start the throwaway test PostgreSQL (tmpfs)
	@docker inspect derp-test-db >/dev/null 2>&1 || docker run -d --name derp-test-db --tmpfs /var/lib/postgresql \
		-e POSTGRES_USER=derp -e POSTGRES_PASSWORD=derp -e POSTGRES_DB=derp_test \
		-p 127.0.0.1:$(TEST_DB_PORT):5432 postgres:18 -c fsync=off -c synchronous_commit=off >/dev/null
	@until docker exec derp-test-db pg_isready -U derp -d derp_test >/dev/null 2>&1; do sleep 0.3; done

db-test-down: ## Remove the test PostgreSQL
	docker rm -f derp-test-db >/dev/null 2>&1 || true

db-dev-up: ## Start a persistent local dev PostgreSQL
	@docker inspect derp-dev-db >/dev/null 2>&1 || docker run -d --name derp-dev-db -v derp-dev-db:/var/lib/postgresql \
		-e POSTGRES_USER=derp -e POSTGRES_PASSWORD=derp -e POSTGRES_DB=derp \
		-p 127.0.0.1:$(DEV_DB_PORT):5432 postgres:18 >/dev/null
	@docker start derp-dev-db >/dev/null
	@until docker exec derp-dev-db pg_isready -U derp -d derp >/dev/null 2>&1; do sleep 0.3; done

migrate: ## alembic upgrade head (DATABASE_URL from .env)
	$(UV) alembic upgrade head

revision: ## New migration: make revision MSG="add x"
	$(UV) alembic revision --autogenerate -m "$(MSG)"

run: ## Run the bot locally (dev bot from .env)
	$(UV) python -m derp

preflight: ## Read-only production checks
	$(UV) python -m derp.ops.preflight

import-main: ## One-off import from the previous Derp database: make import-main SOURCE=postgresql+asyncpg://…
	$(UV) python -m derp.ops.import_main --source "$(SOURCE)"
