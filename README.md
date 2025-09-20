# Derp Telegram Bot

AI-powered friendly Telegram bot built with aiogram.

## Quick Start

### Development

```bash
# Create venv and sync deps (silent)
make venv

# Activate virtual environment
source .venv/bin/activate

# Copy environment file and configure
cp env.example .env
# Edit .env with your actual values

# Run the bot
make run

# Lint and format (all three are synonyms)
make lint
make format
make f

# Run tests
make test
```

### Production Deployment

```bash
# Copy and configure environment
cp env.example .env
# Edit .env with production values

# Build and run with Docker
make docker-up
# Stop containers
make docker-down
```

## Environment Variables

Copy `env.example` to `.env` and configure:

- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token from @BotFather
- `GEL_INSTANCE`: Your Gel (EdgeDB) database instance URL
- `GEL_SECRET_KEY`: Your Gel database secret key
- `OPENAI_API_KEY`: Your OpenAI API key for AI responses
- `LOGFIRE_TOKEN`: Your Logfire token for logging
- `ENVIRONMENT`: Set to "dev" or "prod"
- `IS_DOCKER`: Set to "true" when running in Docker

## Requirements

- Python 3.13+
- uv for dependency management
- Docker (for production deployment)
- Gel (EdgeDB) database
- OpenAI API access
- Logfire account (for logging)

## Common Make Targets

- install: `uv sync`
- venv: create/reuse `.venv` and sync deps (quiet)
- run: `uv run -m derp`
- lint/format/f: `uv run ruff format .` + `uv run ruff check . --fix`
- test: `uv run pytest -q`
- i18n: `make i18n` (extract + update + compile)
- i18n init: `make i18n-init LOCALE=fr`
- gel codegen: `make gel-codegen`
- docker: `make docker-up`, `make docker-down`
