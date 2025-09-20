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

# Lint and format
make lint
make format

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
- lint: `uv run ruff check .`
- format: `uv run ruff format .`
- test: `uv run pytest -q`
- i18n: `make i18n` (extract + update + compile)
- i18n init: `make i18n-init LOCALE=fr`
- gel codegen: `make gel-codegen`
- docker: `make docker-up`, `make docker-down`

## WebApp (Gemini)

- Local server: `http://127.0.0.1:8081/webapp`
- In Telegram, send `/webapp` or use the menu button.

### Dev: auto Cloudflare Tunnel (Makefile only)

Use Make targets (no helper scripts). In `ENVIRONMENT=dev`, this starts a TryCloudflare tunnel, captures the URL, sets `WEBAPP_PUBLIC_BASE`, and runs the bot.

```bash
ENVIRONMENT=dev make run-dev
```

Notes:
- If `WEBAPP_PUBLIC_BASE` is set, no tunnel is started.
- If `cloudflared` is missing, falls back to local run (no public URL).
- In `ENVIRONMENT!=dev`, no tunnel is started.

### VS Code launch.json workflow

VS Code: start the tunnel separately and use the generated env file:

```bash
make tunnel-up
# This writes .env.webapp with WEBAPP_PUBLIC_BASE=<trycloudflare-url>
```

Add both `.env` and `.env.webapp` to `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Derp Bot",
      "type": "python",
      "request": "launch",
      "module": "derp",
      "envFile": ["${workspaceFolder}/.env", "${workspaceFolder}/.env.webapp"],
      "console": "integratedTerminal"
    }
  ]
}
```

Stop the tunnel when finished:

```bash
make tunnel-down
```

Alternatively, run a tunnel manually:

```
WEBAPP_HOST=127.0.0.1 WEBAPP_PORT=8081 make tunnel
```

Or set the public base yourself and run:

```bash
export WEBAPP_PUBLIC_BASE=https://tribe-dryer-idle-book.trycloudflare.com
uv run python -m derp
```

### Production

No Cloudflare in prod. Deploy behind your domain/reverse proxy and set:

```
WEBAPP_PUBLIC_BASE=https://your.domain
```

The bot will open `https://your.domain/webapp`.

The bot sets a chat menu button pointing to `${WEBAPP_PUBLIC_BASE}/webapp`, and the `/webapp` command opens the same URL.
