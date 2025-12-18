# Default goal
.DEFAULT_GOAL := help

# Database URLs for different environments
DATABASE_URL_DEV ?= postgresql+asyncpg://derp:derp@localhost:5432/derp
DATABASE_URL_TEST ?= postgresql+asyncpg://derp_test:derp_test@localhost:5433/derp_test

## =============================================================================
## ENVIRONMENT & DEPENDENCIES
## =============================================================================

## Create or reuse .venv and sync deps quietly
venv:
	@test -d .venv || uv venv --quiet
	@uv sync --quiet
	@echo "To activate the venv in your current shell, run:"
	@echo "  source .venv/bin/activate"

## Install dependencies
install:
	uv sync

## =============================================================================
## RUNNING THE BOT
## =============================================================================

## Run the bot locally
run:
	uv run -m derp

## =============================================================================
## CODE QUALITY
## =============================================================================

## Lint and format code with Ruff
lint format f:
	uv run ruff format .
	uv run ruff check . --fix

## Run type checking (optional, may have errors)
typecheck:
	uv run mypy derp --ignore-missing-imports || true

## =============================================================================
## TESTING
## =============================================================================

## Run tests (quick, no database)
t test:
	uv run pytest -q --ignore=tests/test_db_queries.py --ignore=tests/test_models.py

## Run tests verbosely
test-verbose:
	uv run pytest -v

## Run ALL tests including database tests (requires PostgreSQL)
test-all: db-test-up
	DATABASE_URL=$(DATABASE_URL_TEST) uv run pytest -v
	$(MAKE) db-test-down

## Run only database tests (requires PostgreSQL)
test-db: db-test-up
	DATABASE_URL=$(DATABASE_URL_TEST) uv run pytest -v tests/test_db_queries.py tests/test_models.py
	$(MAKE) db-test-down

## Run tests with coverage
test-cov: db-test-up
	DATABASE_URL=$(DATABASE_URL_TEST) uv run pytest -v --cov=derp --cov-report=html --cov-report=term-missing
	$(MAKE) db-test-down
	@echo "Coverage report: htmlcov/index.html"

## =============================================================================
## DATABASE
## =============================================================================

## Start development PostgreSQL (persistent data)
db-up:
	docker compose up -d db
	@echo "Waiting for PostgreSQL to be ready..."
	@sleep 2
	@echo "PostgreSQL is ready at localhost:5432"

## Stop development PostgreSQL
db-down:
	docker compose down db

## Start test PostgreSQL (ephemeral, in-memory)
db-test-up:
	docker compose up -d db-test
	@echo "Waiting for test PostgreSQL to be ready..."
	@sleep 2
	@echo "Test PostgreSQL is ready at localhost:5433"

## Stop test PostgreSQL
db-test-down:
	docker compose down db-test

## Run database migrations (development)
db-migrate:
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic upgrade head

## Run database migrations (test)
db-migrate-test:
	DATABASE_URL=$(DATABASE_URL_TEST) uv run alembic upgrade head

## Generate a new migration: make db-revision MSG="add new table"
db-revision:
	@if [ -z "$(MSG)" ]; then echo "Usage: make db-revision MSG=\"description\""; exit 1; fi
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic revision --autogenerate -m "$(MSG)"

## Show current migration status
db-status:
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic current

## Rollback last migration
db-downgrade:
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic downgrade -1

## Reset database (drop and recreate all tables) - USE WITH CAUTION
db-reset: db-up
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic downgrade base
	DATABASE_URL=$(DATABASE_URL_DEV) uv run alembic upgrade head

## Open psql shell to development database
db-shell:
	docker exec -it derp-postgres psql -U derp -d derp

## =============================================================================
## I18N (Internationalization)
## =============================================================================

## Run full i18n pipeline (extract -> update -> compile)
i18n:
	$(MAKE) i18n-extract
	$(MAKE) i18n-update
	$(MAKE) i18n-compile

## Extract i18n messages -> derp/locales/messages.pot
i18n-extract:
	uv run pybabel extract -k _:1,1t -k _:1,2 -k __:1,1t -k __:1,2 --input-dirs=derp -o derp/locales/messages.pot --project=derp --version=0.1

## Update .po files from messages.pot
i18n-update:
	uv run pybabel update -d derp/locales -D messages -i derp/locales/messages.pot

## Compile .po to .mo
i18n-compile:
	uv run pybabel compile -d derp/locales -D messages --statistics

## Initialize a new locale: make i18n-init LOCALE=fr
i18n-init:
	@if [ -z "$(LOCALE)" ]; then echo "Usage: make i18n-init LOCALE=<code>"; exit 1; fi
	uv run pybabel init -i derp/locales/messages.pot -d derp/locales -D messages -l $(LOCALE)

## =============================================================================
## DOCKER
## =============================================================================

## Build and start all services with Docker
docker-up:
	docker compose up --build -d

## Stop all services
docker-down:
	docker compose down

## View logs from all services
docker-logs:
	docker compose logs -f

## Rebuild and restart just the bot
docker-restart-bot:
	docker compose up --build -d bot

## =============================================================================
## DEVELOPMENT HELPERS
## =============================================================================

## Set up complete development environment
dev-setup: venv db-up db-migrate
	@echo ""
	@echo "Development environment is ready!"
	@echo ""
	@echo "  1. Activate venv:  source .venv/bin/activate"
	@echo "  2. Copy .env:      cp env.example .env"
	@echo "  3. Edit .env with your credentials"
	@echo "  4. Run bot:        make run"
	@echo ""

## Clean up development environment
dev-clean:
	docker compose down -v
	rm -rf .venv htmlcov .coverage .pytest_cache __pycache__

## =============================================================================
## HELP
## =============================================================================

## Show this help
help:
	@echo "Available targets:"
	@echo ""
	@echo "  Environment & Dependencies:"
	@echo "    venv              Create/reuse .venv and sync deps"
	@echo "    install           Install dependencies (uv sync)"
	@echo ""
	@echo "  Running:"
	@echo "    run               Run the bot locally"
	@echo ""
	@echo "  Code Quality:"
	@echo "    lint/format/f     Lint and format with Ruff"
	@echo "    typecheck         Run mypy type checking"
	@echo ""
	@echo "  Testing:"
	@echo "    test              Run tests (quick, no database)"
	@echo "    test-verbose      Run tests verbosely"
	@echo "    test-all          Run ALL tests including database"
	@echo "    test-db           Run only database tests"
	@echo "    test-cov          Run tests with coverage report"
	@echo ""
	@echo "  Database:"
	@echo "    db-up             Start development PostgreSQL"
	@echo "    db-down           Stop development PostgreSQL"
	@echo "    db-test-up        Start test PostgreSQL (ephemeral)"
	@echo "    db-test-down      Stop test PostgreSQL"
	@echo "    db-migrate        Run migrations (development)"
	@echo "    db-revision       Generate new migration (MSG=...)"
	@echo "    db-status         Show migration status"
	@echo "    db-downgrade      Rollback last migration"
	@echo "    db-reset          Reset database (DANGER!)"
	@echo "    db-shell          Open psql shell"
	@echo ""
	@echo "  I18n:"
	@echo "    i18n              Full i18n pipeline"
	@echo "    i18n-init         Initialize new locale (LOCALE=xx)"
	@echo ""
	@echo "  Docker:"
	@echo "    docker-up         Build and start all services"
	@echo "    docker-down       Stop all services"
	@echo "    docker-logs       View logs"
	@echo ""
	@echo "  Development:"
	@echo "    dev-setup         Set up complete dev environment"
	@echo "    dev-clean         Clean up dev environment"

.PHONY: venv install run lint format f typecheck \
        test test-verbose test-all test-db test-cov \
        db-up db-down db-test-up db-test-down db-migrate db-migrate-test \
        db-revision db-status db-downgrade db-reset db-shell \
        i18n i18n-extract i18n-update i18n-compile i18n-init \
        docker-up docker-down docker-logs docker-restart-bot \
        dev-setup dev-clean help
