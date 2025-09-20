SHELL := /bin/bash

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

## Run the bot with auto Cloudflare Tunnel (dev)
run-dev:
	@set -euo pipefail; \
	LOCAL_HOST="$${WEBAPP_HOST:-127.0.0.1}"; \
	LOCAL_PORT="$${WEBAPP_PORT:-8081}"; \
	LOCAL_URL="http://$${LOCAL_HOST}:$${LOCAL_PORT}"; \
	if [[ "${ENVIRONMENT:-dev}" != "dev" ]]; then \
		echo "ENVIRONMENT != dev → running without tunnel"; \
		uv run -m derp; \
		exit $$?; \
	fi; \
	if [[ -n "${WEBAPP_PUBLIC_BASE:-}" ]]; then \
		echo "WEBAPP_PUBLIC_BASE already set: ${WEBAPP_PUBLIC_BASE}"; \
		uv run -m derp; \
		exit $$?; \
	fi; \
	if ! command -v cloudflared >/dev/null 2>&1; then \
		echo "cloudflared not found. Install it or set WEBAPP_PUBLIC_BASE to a public URL."; \
		echo "Falling back to local run (no public URL)."; \
		uv run -m derp; \
		exit $$?; \
	fi; \
	echo "Starting Cloudflare tunnel to $$LOCAL_URL"; \
	LOG_FILE=$$(mktemp -t derp-cf.XXXX.log); \
	PID_FILE=$$(mktemp -t derp-cf.XXXX.pid); \
	trap 'if [[ -f $$PID_FILE ]]; then kill $$(cat $$PID_FILE) 2>/dev/null || true; rm -f $$PID_FILE; fi; rm -f $$LOG_FILE' EXIT; \
	(cloudflared tunnel --no-autoupdate --url "$$LOCAL_URL" >"$$LOG_FILE" 2>&1 & echo $$! > "$$PID_FILE"); \
	URL=""; \
	for i in {1..200}; do \
		URL=$$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$$LOG_FILE" | head -n1 || true); \
		[[ -n "$$URL" ]] && break; \
		sleep 0.1; \
	done; \
	if [[ -z "$$URL" ]]; then \
		echo "Failed to detect TryCloudflare URL. See $$LOG_FILE"; \
		uv run -m derp; \
		exit $$?; \
	fi; \
	echo "Tunnel ready → $$URL"; \
	WEBAPP_PUBLIC_BASE="$$URL" uv run -m derp

## Start a background Cloudflare Tunnel and capture URL to $(CF_URL) and $(CF_ENV)
tunnel-up:
	@set -euo pipefail; \
	mkdir -p $(CF_STATE_DIR); \
	LOCAL_HOST="$${WEBAPP_HOST:-127.0.0.1}"; \
	LOCAL_PORT="$${WEBAPP_PORT:-8081}"; \
	LOCAL_URL="http://$${LOCAL_HOST}:$${LOCAL_PORT}"; \
	if ! command -v cloudflared >/dev/null 2>&1; then \
		echo "cloudflared not found. Please install it first."; \
		exit 1; \
	fi; \
	# Stop previous tunnel if exists
	if [[ -f "$(CF_PID)" ]]; then \
		PID=$$(cat "$(CF_PID)" || true); \
		if [[ -n "$$PID" ]] && kill -0 "$$PID" 2>/dev/null; then \
			echo "Stopping previous tunnel (PID $$PID)…"; \
			kill "$$PID" || true; \
			sleep 0.5; \
		fi; \
	fi; \
	: > "$(CF_LOG)"; \
	(cloudflared tunnel --no-autoupdate --url "$$LOCAL_URL" >"$(CF_LOG)" 2>&1 & echo $$! > "$(CF_PID)"); \
	echo "Starting tunnel → $$LOCAL_URL (logs: $(CF_LOG))"; \
	URL=""; \
	for i in {1..200}; do \
		URL=$$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$(CF_LOG)" | head -n1 || true); \
		[[ -n "$$URL" ]] && break; \
		sleep 0.1; \
	done; \
	if [[ -z "$$URL" ]]; then \
		echo "Failed to detect TryCloudflare URL. See $(CF_LOG)"; \
		exit 2; \
	fi; \
	echo "$$URL" > "$(CF_URL)"; \
	echo "WEBAPP_PUBLIC_BASE=$$URL" > "$(CF_ENV)"; \
	echo "Tunnel ready: $$URL"; \
	echo "Wrote $(CF_ENV) (use as VS Code envFile)"

## Stop background tunnel
tunnel-down:
	@set -euo pipefail; \
	if [[ -f "$(CF_PID)" ]]; then \
		PID=$$(cat "$(CF_PID)" || true); \
		if [[ -n "$$PID" ]] && kill -0 "$$PID" 2>/dev/null; then \
			echo "Stopping tunnel (PID $$PID)…"; \
			kill "$$PID" || true; \
		fi; \
		rm -f "$(CF_PID)"; \
	fi; \
	true

## Start a quick Cloudflare Tunnel and print URL (foreground)
tunnel:
	@set -e; \
	if ! command -v cloudflared >/dev/null 2>&1; then \
		echo "cloudflared not found. Please install it first."; \
		exit 1; \
	fi; \
	cloudflared tunnel --url "http://$${WEBAPP_HOST:-127.0.0.1}:$${WEBAPP_PORT:-8081}"

## Print the WebApp URL (public if available)
print-webapp-url:
	@set -e; \
	if [[ -n "${WEBAPP_PUBLIC_BASE}" ]]; then \
		echo "${WEBAPP_PUBLIC_BASE}/webapp"; \
		exit 0; \
	fi; \
	if [[ -f "$(CF_URL)" ]]; then \
		URL=$$(cat "$(CF_URL)" || true); \
		if [[ -n "$$URL" ]]; then echo "$$URL/webapp"; exit 0; fi; \
	fi; \
	echo "http://$${WEBAPP_HOST:-127.0.0.1}:$${WEBAPP_PORT:-8081}/webapp"

## Lint code with Ruff
lint:
	uv run ruff check . --fix

## Format code with Ruff
format:
	uv run ruff format .

## Run tests (quiet)
test:
	uv run pytest -q

## Run tests (verbose)
test-verbose:
	uv run pytest -v
CF_STATE_DIR := .cfdev
CF_PID := $(CF_STATE_DIR)/cloudflared.pid
CF_LOG := $(CF_STATE_DIR)/cloudflared.log
CF_URL := $(CF_STATE_DIR)/webapp.url
CF_ENV := .env.webapp


## Run full i18n pipeline (extract -> update -> compile)
i18n:
	$(MAKE) i18n-extract
	$(MAKE) i18n-update
	$(MAKE) i18n-compile

## Extract i18n messages -> derp/locales/messages.pot
i18n-extract:
	uv run pybabel extract -k _:1,1t -k _:1,2 --input-dirs=derp -o derp/locales/messages.pot --project=derp --version=0.1

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
	uv run gel-py

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
	@echo "  run-dev        Run with auto Cloudflare Tunnel (dev)"
	@echo "  tunnel         Start a quick Cloudflare Tunnel (foreground)"
	@echo "  tunnel-up      Start background tunnel and capture URL to $(CF_ENV)"
	@echo "  tunnel-down    Stop background tunnel"
	@echo "  print-webapp-url  Print the WebApp URL"
	@echo "  lint           Lint with Ruff"
	@echo "  format         Format with Ruff"
	@echo "  test           Run tests (quiet)"
	@echo "  test-verbose   Run tests (verbose)"
	@echo "  i18n           Extract, update, and compile translations"
	@echo "  i18n-init      Initialize new locale (LOCALE=xx)"
	@echo "  gel-codegen    Generate Gel query bindings"
	@echo "  docker-up      Build and start with Docker"
	@echo "  docker-down    Stop Docker services"

.PHONY: venv install activate run run-dev tunnel print-webapp-url lint format test test-verbose i18n i18n-extract i18n-update i18n-compile i18n-init gel-codegen docker-up docker-down help
