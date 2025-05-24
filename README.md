# Derp Telegram Bot

AI-powered friendly Telegram bot built with aiogram and Pydantic AI.

## Features

- **Class-Based Handlers**: Uses aiogram's class-based MessageHandler for clean, structured code
- **Modular AI Service**: Reusable AI service designed for future agentic tool integration
- **Context-Aware Responses**: Analyzes reply-to messages to provide relevant answers
- **Multi-language Support**: Works with both English ("derp") and Russian ("дерп")
- **Configurable AI Models**: Uses OpenAI o3-mini by default, with Groq Llama 3.1 fallback

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
