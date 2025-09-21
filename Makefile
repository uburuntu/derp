# Default goal
.DEFAULT_GOAL := help

## Create or reuse .venv and sync deps quietly
venv:
	@test -d .venv || uv venv --quiet
	@uv sync --quiet
	@echo "To activate the venv in your current shell, run:"
	@echo "  source .venv/bin/activate"

## Install dependencies
install:
	uv sync

## Run the bot locally
run:
	uv run -m derp

## Lint and format code with Ruff
lint format f:
	uv run ruff format .
	uv run ruff check . --fix

## Run tests (quiet)
t test tests:
	uv run pytest -q

## Run tests (verbose)
test-verbose:
	uv run pytest -v

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

## Generate Gel query code
gel-codegen:
	uv run gel-py -I uburuntu/derp

## Build and start services with Docker
docker-up:
	docker compose up --build -d

## Stop services
docker-down:
	docker compose down

## Show this help
help:
	@echo "Available targets:"
	@echo "  venv           Create/reuse .venv and sync deps (quiet)"
	@echo "  install        Install dependencies (uv sync)"
	@echo "  run            Run the bot locally"
	@echo "  lint/format/f  Lint and format with Ruff"
	@echo "  test           Run tests (quiet)"
	@echo "  test-verbose   Run tests (verbose)"
	@echo "  i18n           Extract, update, and compile translations"
	@echo "  i18n-init      Initialize new locale (LOCALE=xx)"
	@echo "  gel-codegen    Generate Gel query bindings"
	@echo "  docker-up      Build and start with Docker"
	@echo "  docker-down    Stop Docker services"

.PHONY: venv install activate run lint format f test test-verbose i18n i18n-extract i18n-update i18n-compile i18n-init gel-codegen docker-up docker-down help
