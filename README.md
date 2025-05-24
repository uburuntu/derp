# Derp Telegram Bot

AI-powered friendly Telegram bot built with aiogram and Pydantic AI.

## Features

- **Class-Based Handlers**: Uses aiogram's class-based MessageHandler for clean, structured code
- **Modular AI Service**: Reusable AI service designed for future agentic tool integration
- **Context-Aware Responses**: Analyzes reply-to messages to provide relevant answers
- **Multi-language Support**: Works with both English ("derp") and Russian ("дерп")
- **Configurable AI Models**: Uses OpenAI o3-mini by default, with Groq Llama 3.1 fallback

## Quick Start

### Development

```bash
# Install dependencies
uv sync

# Copy environment file and configure
cp env.example .env
# Edit .env with your actual values

# Run the bot
uv run -m derp

# Format code
uv run black .

# Lint code
uv run ruff check .

# Run tests
uv run pytest tests/ -v
```

### Production Deployment

```bash
# Copy and configure environment
cp env.example .env
# Edit .env with production values

# Deploy using the provided script
./scripts/deploy.sh
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
