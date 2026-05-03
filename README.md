# Derp

Derp is a Telegram AI assistant for private chats and groups. It combines Gemini chat, web search, media generation, reminders, chat memory, and Telegram Stars credits in a Bun + grammY bot.

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
- **Credit Economy** — subscriptions via Telegram Stars, top-up packs, shared group credits
- **Inline Mode** — use `@BotUsername query` in any chat
- **Settings Menu** — response style, language, permissions, memory management
- **i18n** — English and Russian, auto-detect from user language
- **Observability** — structured logging and tracing via Logfire + OpenTelemetry

## Run Locally

### Bun App With Docker Postgres

```bash
# Clone and install
git clone https://github.com/uburuntu/derp.git
cd derp
bun install

# Configure
cp .env.example .env
# Edit .env with your tokens

# Start PostgreSQL
docker compose up -d db

# Apply database migrations
bunx drizzle-kit migrate

# Run the bot
bun run src/index.ts
```

The host-local `DATABASE_URL` uses `localhost:5433`.

### Full Docker Compose

For a container-only run, copy `.env.example` to `.env`, fill in the required tokens, then run migrations before starting the bot:

```bash
docker compose up -d db
docker compose run --rm --build bot bunx drizzle-kit migrate
docker compose up --build bot
```

Docker Compose overrides the bot container database URL to use the internal `db:5432` service address.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `GOOGLE_API_KEY` | Yes | Google AI API key |
| `BOT_USERNAME` | No | Bot username (default: DerpRobot) |
| `ENVIRONMENT` | No | `dev` or `prod` (default: dev) |
| `HEALTH_PORT` | No | HTTP health server port (default: 8080) |
| `GOOGLE_API_KEYS` | No | Comma-separated extra API keys for round-robin |
| `GOOGLE_API_PAID_KEY` | No | Paid API key for image/video generation |
| `BRAVE_SEARCH_API_KEY` | No | Brave Search API key (falls back to DuckDuckGo) |
| `BOT_ADMIN_IDS` | No | Comma-separated admin Telegram user IDs |
| `BOT_ADMIN_EVENTS_CHAT_ID` | No | Chat ID for payment/startup notifications |
| `LOGFIRE_TOKEN` | No | Logfire write token for observability |
| `REMINDER_CHECK_INTERVAL_MS` | No | Scheduler poll interval (default: 60000) |
| `POSTGRES_USER` | No | Docker Compose Postgres user (default: derp) |
| `POSTGRES_PASSWORD` | No | Docker Compose Postgres password (default: derp) |
| `POSTGRES_DB` | No | Docker Compose Postgres database (default: derp) |
| `POSTGRES_PORT` | No | Host port for Docker Compose Postgres (default: 5433) |
| `POSTGRES_TEST_DB` | No | Docker Compose test database (default: derp_test) |
| `POSTGRES_TEST_PORT` | No | Host port for Docker Compose test Postgres (default: 5434) |

## Architecture

Product behavior, pricing boundaries, and open decisions are defined in [docs/PRD.md](docs/PRD.md). Update that file before changing tiers, tool access, onboarding promises, or credit behavior.

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
2. Rate limiter (3 msgs / 2s per user, before DB work)
3. Sequentialize (per-chat concurrency control)
4. Logger (root OTEL span per update)
5. Hydrator (upsert user/chat/member/message)
6. Session (credit balances, tier determination)
7. Auto chat action ("typing..." indicators)
8. i18n (locale detection)

### Tool System

The tool registry is the single source of truth. It registers pricing for every tool, command handlers and `/help` entries for tools with `commands`, and model-callable schemas only for tools marked `allowAutoCall`.

Tools that spend credits, generate media, write memory, or create reminders must not set `allowAutoCall`; expose them through explicit slash commands or a confirmation flow.

### Credit Tiers

| Tier | Model | Context | Condition |
|------|-------|---------|-----------|
| FREE | Gemini Flash Lite | 15 messages | No credits, no subscription |
| STANDARD | Gemini Flash | 100 messages | Has credits or active subscription |

## Development

```bash
# Run tests, type check, and Biome
bun run check

# Run tests only
bun run test

# Type check only
bun run typecheck

# Lint only
bun run lint

# Format and apply safe fixes
bun run format
```

### Database Migrations

Schema changes are managed with Drizzle migrations in `drizzle/`. Do not run `drizzle-kit push --force` in application startup or production deploys.

```bash
# Generate a migration after editing src/db/schema.ts
bunx drizzle-kit generate

# Apply pending migrations
bunx drizzle-kit migrate
```

Existing databases that were previously managed with `drizzle-kit push` need a one-time migration baseline before generated migrations are used in production.

## Deployment

```bash
docker compose up -d
```

The Dockerfile uses `oven/bun:1` with ffmpeg for audio conversion and includes `drizzle-kit`. Run `bunx drizzle-kit migrate` from the built image before starting the bot. The `/health` endpoint returns 200 only after the bot, scheduler, database connection, and expected schema are ready.

## License

MIT
