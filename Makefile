.PHONY: help setup dev run stop check lint format test typecheck db db-stop db-push db-studio db-reset clean docker-build docker-up docker-down logs

# ── Help ─────────────────────────────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help

# ── First-Time Setup ─────────────────────────────────────────────────────────

setup: ## First-time setup: install deps, copy env, start DB, push schema
	@echo "Installing dependencies..."
	bun install
	@test -f .env || (cp .env.example .env && echo "Created .env — fill in your tokens")
	@echo "Starting PostgreSQL (waiting for healthy)..."
	docker compose up -d db --wait
	@echo "Pushing schema..."
	bunx drizzle-kit push --force
	@echo ""
	@echo "Done! Edit .env with your TELEGRAM_BOT_TOKEN and GOOGLE_API_KEY, then run: make dev"

# ── Development ──────────────────────────────────────────────────────────────

dev: ## Start bot in dev mode with auto-restart on file changes
	@grep -q "TELEGRAM_BOT_TOKEN" .env 2>/dev/null || (echo "Error: .env is missing or uses old format. Run:\n  cp .env.example .env\nThen fill in your tokens." && exit 1)
	bun --watch run src/index.ts

run: ## Start bot (no watch mode)
	bun run src/index.ts

# ── Code Quality ─────────────────────────────────────────────────────────────

check: lint typecheck test ## Run all checks (lint + typecheck + test)

lint: ## Lint and check formatting
	bunx @biomejs/biome check src/ tests/

format: ## Auto-fix lint and formatting issues
	bunx @biomejs/biome check --write src/ tests/

typecheck: ## Run TypeScript type checker
	bunx tsc --noEmit

test: ## Run unit tests
	bun test

test-watch: ## Run tests in watch mode
	bun test --watch

# ── Database ─────────────────────────────────────────────────────────────────

db: ## Start PostgreSQL container
	docker compose up -d db

db-stop: ## Stop PostgreSQL container
	docker compose stop db

db-push: ## Push schema changes to database
	bunx drizzle-kit push --force

db-studio: ## Open Drizzle Studio (visual DB browser)
	bunx drizzle-kit studio

db-reset: ## Drop and recreate all tables (DESTRUCTIVE)
	@echo "This will destroy all data. Press Ctrl+C to cancel."
	@sleep 3
	docker compose stop db
	docker compose rm -f db
	docker volume rm derp_derp-pgdata 2>/dev/null || true
	docker compose up -d db --wait
	bunx drizzle-kit push --force
	@echo "Database reset complete."

# ── Docker (Production) ─────────────────────────────────────────────────────

docker-build: ## Build Docker image
	docker compose build bot

docker-up: ## Start bot + DB in Docker
	docker compose up -d

docker-down: ## Stop all Docker containers
	docker compose down

logs: ## Tail bot container logs
	docker compose logs -f bot

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean: ## Remove node_modules and build artifacts
	rm -rf node_modules dist
	@echo "Cleaned. Run 'bun install' to reinstall."
