# Derp

AI-powered Telegram bot built with Bun, grammY, Drizzle ORM, and Google Gemini.

## Features

- **Conversational AI** — mention the bot or reply to trigger responses via Gemini Flash
- **Image Generation** (`/imagine`) — generate images from text prompts
- **Image Editing** (`/edit`) — edit images with text instructions
- **Video Generation** (`/video`) — create short videos with Veo 3.1
- **Text-to-Speech** (`/tts`) — convert text to voice messages
- **Deep Reasoning** (`/think`) — extended thinking with Gemini Pro
- **Web Search** (`/search`) — search the web via Brave or DuckDuckGo
- **Reminders** (`/remind`) — one-time and recurring reminders with cron
- **Chat Memory** — persistent per-chat memory the bot learns from
- **Credit Economy** — subscriptions via Telegram Stars, top-up packs, group pools
- **Inline Mode** — use `@BotUsername query` in any chat
- **Settings Menu** — personality, language, permissions, memory management
- **i18n** — English and Russian, auto-detect from user language
- **Observability** — structured logging and tracing via Logfire + OpenTelemetry

## Quick Start

```bash
# Clone and install
git clone https://github.com/AviaryLabs/derp.git
cd derp
bun install

# Configure
cp .env.example .env
# Edit .env with your tokens

# Start PostgreSQL
docker compose up -d db

# Push database schema
bunx drizzle-kit push

# Run the bot
bun run src/index.ts
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `GOOGLE_API_KEY` | Yes | Google AI API key |
| `BOT_USERNAME` | No | Bot username (default: DerpRobot) |
| `ENVIRONMENT` | No | `dev` or `prod` (default: dev) |
| `GOOGLE_API_KEYS` | No | Comma-separated extra API keys for round-robin |
| `GOOGLE_API_PAID_KEY` | No | Paid API key for image/video generation |
| `BRAVE_SEARCH_API_KEY` | No | Brave Search API key (falls back to DuckDuckGo) |
| `BOT_ADMIN_IDS` | No | Comma-separated admin Telegram user IDs |
| `BOT_ADMIN_EVENTS_CHAT_ID` | No | Chat ID for payment/startup notifications |
| `LOGFIRE_TOKEN` | No | Logfire write token for observability |
| `REMINDER_CHECK_INTERVAL_MS` | No | Scheduler poll interval (default: 60000) |

## Architecture

```
src/
├── bot/           # Bot setup, custom context type
├── common/        # Shared utilities (reply, extractor, ffmpeg, observability)
├── credits/       # Credit economy (service, subscriptions, packs, UI)
├── db/            # Database schema, connection, query modules
├── handlers/      # Telegram command & message handlers
├── i18n/          # Locale files (en.ftl, ru.ftl)
├── llm/           # LLM integration (registry, context builder, Google provider)
├── middleware/     # Middleware stack (error, logger, hydrator, session, rate limiter)
├── scheduler/     # Reminder scheduler with cron support
├── tools/         # Tool definitions (9 tools with credit gating)
└── index.ts       # Entry point
```

### Middleware Stack

1. Error boundary
2. Sequentialize (per-chat concurrency control)
3. Logger (root OTEL span per update)
4. Hydrator (upsert user/chat/member/message)
5. Session (credit balances, tier determination)
6. Auto chat action ("typing..." indicators)
7. Rate limiter (3 msgs / 2s per user)
8. i18n (locale detection)

### Tool System

Tools are defined once and automatically generate: slash commands, LLM function schemas, `/help` entries, and pricing. Adding a tool = one file in `src/tools/`.

### Credit Tiers

| Tier | Model | Context | Condition |
|------|-------|---------|-----------|
| FREE | Gemini Flash Lite | 15 messages | No credits, no subscription |
| STANDARD | Gemini Flash | 100 messages | Has credits or active subscription |

## Development

```bash
# Type check
bunx tsc --noEmit

# Lint & format
bunx @biomejs/biome check --write src/

# Run tests
bun test
```

## Deployment

```bash
docker compose up -d
```

The Dockerfile uses `oven/bun:1` with ffmpeg for audio conversion.

## License

MIT
