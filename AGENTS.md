# Repository Guidelines

## Project Structure & Module Organization
- `derp/`: Main package (entrypoint `__main__.py`).
- `derp/handlers/`, `derp/middlewares/`, `derp/filters/`: Telegram logic split by concern. Add new handlers under `derp/handlers/` and include their router in `derp/__main__.py`.
- `derp/common/`: Shared services (DB, utils, LLM integration).
- `derp/queries/`: Generated EdgeQL/Gel query helpers. Update via codegen when schema changes.
- `derp/locales/`: i18n sources (`.po/.pot`) and compiled `.mo` files.
- `tests/`: Pytest suite (async-friendly).
- `scripts/`: Utilities for i18n and Gel codegen.
- `dbschema/`: Gel database schema and migrations.

## Build, Test, and Development Commands
- Install deps: `uv sync`
- Run bot locally: `uv run -m derp`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .` (or `uv run black .` if preferred locally)
- Tests: `uv run pytest -q`
- i18n: `./scripts/i18n_extract.sh`, `./scripts/i18n_update.sh`, `./scripts/i18n_compile.sh`
- Gel codegen: `./scripts/gel_codegen.sh`
- Docker (optional): `docker compose up --build -d`

## Coding Style & Naming Conventions
- Python 3.13+, 4‑space indentation, type hints required.
- Naming: modules/functions `snake_case`, classes `CamelCase`, constants `UPPER_SNAKE`.
- Imports: prefer absolute within `derp.*`.
- Keep handlers small; place cross‑cutting logic in `middlewares/` or `common/`.
- Lint/format before pushing; CI runs Ruff check and format validation.

## Testing Guidelines
- Frameworks: `pytest`, `pytest-asyncio`.
- Name tests `tests/test_*.py`; use async tests for coroutine code.
- Aim to cover filters, handlers’ pure logic, and utilities; stub Telegram objects with `MagicMock`.
- Run locally with `uv run pytest -v`.

## Commit & Pull Request Guidelines
- Use Conventional Commits where possible: `feat:`, `fix:`, `chore:`, `refactor:`, etc. Example: `fix: streamline reply handling`.
- PRs: include what/why, linked issues, and screenshots/log snippets if behavior changes.
- Requirements: passing CI (Ruff + tests), updated docs/i18n/queries when applicable.

## Security & Configuration
- Do not commit secrets. Use `env.example` to populate `.env`/`.env.prod`.
- Required env: Telegram token, Gel DSN/secret, OpenAI/Google/OpenRouter keys, `LOGFIRE_TOKEN`, `ENVIRONMENT`.
- Production containers run non‑root; prefer read‑only FS and minimal privileges.
