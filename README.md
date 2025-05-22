# Derp Telegram Bot

AI-powered friendly Telegram bot built with aiogram.

## Quick Start

```bash
# Install dependencies
uv pip install --system -r pyproject.toml --all-extras

# Run the bot
uv run -m derp

# Format code
uv run black .

# Lint code
uv run ruff check .
```

## Development

- Python 3.13+
- Uses uv for dependency management
- Ruff for linting and formatting
- Docker support included

## Environment Variables

Copy `.env.example` to `.env` and configure:

- `BOT_TOKEN`: Your Telegram bot token
- Other settings as needed
