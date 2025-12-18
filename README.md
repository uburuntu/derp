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

## Configuration

Key environment variables in `.env`:

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `DATABASE_URL` | PostgreSQL connection string |
| `GOOGLE_API_KEY` | Google AI API key |
| `LOGFIRE_TOKEN` | Logfire observability token |
| `ENVIRONMENT` | `dev` or `prod` |

See `env.example` for the complete list.

## Commands

Run `make help` to see all available targets.
