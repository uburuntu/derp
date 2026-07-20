# Derp

AI-powered Telegram bot built with [aiogram](https://github.com/aiogram/aiogram) and [Gemini](https://ai.google.dev/).

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv)
- Docker (for PostgreSQL)
- Telegram bot token ([@BotFather](https://t.me/BotFather))
- Google API key

## Setup

```bash
cp env.example .env   # Configure your credentials
make dev-setup        # Creates venv, starts DB, runs migrations
make run              # Start the bot
```

The PostgreSQL 18 Compose service uses the `postgres-data-v18` volume. Export
and restore any data needed from the former `postgres-data` volume before
removing it; PostgreSQL 17 data directories are not binary-compatible with 18.

## Configuration

Key environment variables in `.env`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_URL` | PostgreSQL connection string |
| `GOOGLE_API_PAID_KEY` | Google AI API key |
| `LOGFIRE_TOKEN` | Logfire observability token |
| `LOGFIRE_CAPTURE_AI_CONTENT` | Local-only opt-in for Pydantic AI text capture (default `false`) |
| `ENVIRONMENT` | `dev` or `prod` |

See `env.example` for the complete list.

See [docs/observability.md](docs/observability.md) for telemetry ownership,
content-capture policy, privacy boundaries, and shutdown behavior.

## Commands

Run `make help` to see all available targets.

Cross-cutting work deferred from cleanup is tracked in
[`docs/architecture-roadmap.md`](docs/architecture-roadmap.md).
