# Derp PRD v2 — Part 3: Implementation

---

## 11. Configuration

### 11.1 Environment Variables

```bash
# ── Required ──────────────────────────────────────────────

ENVIRONMENT=dev|prod                    # Deployment environment

# Telegram
TELEGRAM_BOT_TOKEN=                     # From @BotFather
BOT_USERNAME=DerpRobot                  # Without @

# Database
DATABASE_URL=postgresql://user:pass@host:5432/derp

# Google AI
GOOGLE_API_KEY=                         # Primary Gemini API key
GOOGLE_API_KEYS=key1,key2,key3          # Additional keys for round-robin
GOOGLE_API_PAID_KEY=                    # High-quota production key

# Search
BRAVE_SEARCH_API_KEY=                   # Brave Search API key (free tier: 1 req/sec)
                                        # If not set, falls back to DuckDuckGo (less reliable)

# ── Optional ──────────────────────────────────────────────

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=           # OpenTelemetry collector
OTEL_SERVICE_NAME=derp                  # Service name in traces

# Access Control
BOT_ADMIN_IDS=28006241                  # Comma-separated bot owner Telegram IDs
                                        # (distinct from Telegram chat admins)

# Scheduler
REMINDER_CHECK_INTERVAL_MS=60000        # How often to check for due reminders
```

### 11.2 Config Validation

All configuration parsed and validated at startup with Zod:

```typescript
const configSchema = z.object({
  environment: z.enum(['dev', 'prod']),
  telegramBotToken: z.string().min(1),
  botUsername: z.string().default('DerpRobot'),
  databaseUrl: z.string().url(),
  googleApiKey: z.string().min(1),
  googleApiKeys: z.string().transform(s => s.split(',')).default(''),
  googleApiPaidKey: z.string().optional(),
  braveSearchApiKey: z.string().optional(),  // Falls back to DuckDuckGo if not set
  botAdminIds: z.string().transform(s => s.split(',').map(Number)).default(''),
  reminderCheckIntervalMs: z.coerce.number().default(60000),
})
```

Missing required values → immediate crash with clear error. No silent defaults for secrets.

---

## 12. Internationalization (i18n)

### 12.1 Strategy

- Use `@grammyjs/i18n` with Fluent message format (`.ftl` files)
- Default locale: English (`en`)
- Launch locales: English, Russian (`ru`)
- Locale resolution: user's `language_code` → chat override → `en` fallback
- Bot command descriptions registered per-locale via `setMyCommands`

### 12.2 Scope

**i18n applies to:**
- All bot responses (help text, error messages, confirmations, onboarding)
- Credit/payment UI (balance, buy flow, receipts)
- Settings menu labels
- Command descriptions (in Telegram's command list)
- Tool unavailability messages

**i18n does NOT apply to:**
- LLM-generated content (system prompt instructs LLM to match user's language)
- Bot admin commands (English only)
- Log messages (English only)

### 12.3 Example Fluent File (`en.ftl`)

```fluent
help-title = 🤖 Derp Commands
help-chat = Just mention me (@{ $botUsername }) or say "derp" — I'll respond!
help-imagine = /imagine <prompt> — Generate an image ({ $freeDaily } free/day, then { $credits } credits)

credits-balance = 💰 Balance: { $amount } credits
credits-used = ✨ { $cost } credits used · { $remaining } remaining
credits-low = ⚠️ { $cost } credits used · { $remaining } remaining · /buy to top up
credits-empty = Your credits have run out. /buy to get more!

error-rate-limited = The AI service is busy right now. Try again in 30 seconds.
error-generic = Something went wrong. I couldn't process that.

reminder-created = ⏰ Reminder set! I'll remind you { $when }.
reminder-fired = 🔔 Reminder: { $text }
reminder-fired-delayed = 🔔 (delayed — bot was restarting) Reminder: { $text }
```

---

## 13. Observability

### 13.1 Structured Logging

Every significant action emits a structured log with consistent attributes:

```typescript
logger.info('tool_executed', {
  userId: ctx.user.telegramId,
  chatId: ctx.chat.telegramId,
  tool: 'imagine',
  tier: 'STANDARD',
  creditsDeducted: 10,
  creditsRemaining: 240,
  durationMs: 1823,
})
```

### 13.2 OpenTelemetry Tracing

Spans for:
- **Root span:** Full update processing (message → response)
- **Middleware spans:** Each middleware execution
- **LLM spans:** Gemini API calls with `gen_ai.*` semantic attributes
- **Tool spans:** Tool execution with credit info
- **DB spans:** Drizzle queries
- **Scheduler spans:** Reminder checks and executions

Attributes follow OpenTelemetry semantic conventions:
- `gen_ai.system` = `google`
- `gen_ai.request.model` = `gemini-2.5-flash`
- `gen_ai.usage.input_tokens` = 1234
- `gen_ai.usage.output_tokens` = 567
- `gen_ai.usage.cache_hit_tokens` = 890

### 13.3 Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `derp.updates` | Counter | `type` | Updates processed by type |
| `derp.llm.requests` | Counter | `model`, `tier` | LLM API calls |
| `derp.llm.tokens.input` | Histogram | `model` | Input token distribution |
| `derp.llm.tokens.output` | Histogram | `model` | Output token distribution |
| `derp.llm.cache_hit_ratio` | Gauge | `model` | Cache hit rate (from Gemini usage data) |
| `derp.tools.calls` | Counter | `tool`, `outcome` | Tool invocations |
| `derp.credits.transactions` | Counter | `type` | Credit movements |
| `derp.credits.revenue` | Counter | `source` | Stars received (purchase, subscription) |
| `derp.reminders.fired` | Counter | `recurring`, `uses_llm` | Reminders executed |
| `derp.context.tokens` | Histogram | `tier` | Context window size |

### 13.4 Reaction Tracking

When users react to bot messages with 👍 or 👎 (via `message_reaction` updates), log with correlation to the original LLM request:

```typescript
logger.info('user_feedback', {
  messageId: reaction.messageId,
  chatId: reaction.chatId,
  emoji: '👍',
  // Correlate with message metadata for quality analysis
})
```

---

## 14. Error Handling & Resilience

### 14.1 Telegram API

- **Retry transient errors** (network, 5xx) up to 3 times with exponential backoff
- **Respect `Retry-After`** on 429 responses
- **Fallback to plain text** on MarkdownV2 parse errors (strip formatting, retry)
- **Graceful media fallback:** if photo/video send fails, fall back to text-only response
- Use grammY's `auto-retry` transformer plugin for automatic retry handling

### 14.2 LLM API

- **Timeout:** 30s for chat, 60s for image, 180s for video
- **Retry:** 1 retry on transient Google API errors (5xx, network)
- **Rate limiting:** Round-robin across `GOOGLE_API_KEYS` for request distribution
- **Model refusal:** Catch safety filter blocks → friendly message to user
- **Empty response:** React with 👌 emoji instead of sending empty text

### 14.3 Data Integrity

- **Idempotency keys** on all credit transactions (prevent double-charging on retries)
- **Unique constraints** on `(chat_id, telegram_message_id)` for messages
- **Soft deletes** for messages (set `deleted_at`, never hard-delete)
- **CHECK constraints** on credit balances (`credits >= 0`)
- **Transaction-scoped** credit mutations (read balance + deduct in one transaction)

### 14.4 Scheduler Resilience

- Reminders are persisted in PostgreSQL (survive bot restarts)
- On startup: load all overdue reminders and fire immediately with a `(delayed)` note
- Failed reminder execution: retry once, then mark as `failed` with error in metadata
- Cron reminders: if missed, fire once on recovery (not all missed occurrences)

### 14.5 API Call Caching

`getChat`, `getChatMember`, and `getChatAdministrators` results are cached in the database:
- `chats.cached_at` and `chat_members.cached_at` track freshness
- Refreshed every 24 hours or on `chat_member` update events
- Prevents rate limiting from Telegram API (these calls are throttled)

---

## 15. Testing Strategy

### 15.1 Unit Tests

- Credit calculations, tier selection, cost computation
- Tool access checking (free limits, credit deduction logic)
- Context builder (compact format generation, participant scoping, forum topic filtering)
- Config validation (Zod schema edge cases)
- Cron expression parsing for reminders
- Message format sanitization (MarkdownV2 escaping)
- Decoy pricing math (verify pack/subscription relationships)

### 15.2 Integration Tests

- Database operations: CRUD for all tables, credit queries, usage quota tracking
- Middleware pipeline: context enrichment, credit service injection
- Payment flow: invoice creation → pre-checkout → credit grant → refund
- Subscription flow: subscribe → monthly credit grant → cancel
- Credit transfer: personal → group pool (min 100 enforcement)
- Idempotency: duplicate transactions, concurrent deductions
- Reminder lifecycle: create → fire → complete (one-time), create → fire → reschedule (recurring)
- Chat member tracking: optimistic upsert, role updates, active/inactive transitions

### 15.3 End-to-End Tests

- Full message flow: mock Telegram update → middleware → handler → LLM mock → response → metadata stored
- Command handling: each command + aliases with expected inputs/outputs
- Credit lifecycle: subscribe → use tools → check balance → top up → transfer to group → verify ledger
- Menu interaction: `/settings` → navigate menu → change personality/permissions → verify DB
- Tool-command duality: verify `/imagine` and agent `imagine` tool produce same results
- `/info` command: verify metadata is stored and retrieved correctly
- Inline mode: verify placeholder → edit flow
- Forum topics: verify context scoping to thread_id

### 15.4 Test Infrastructure

- **Runner:** `bun test` (built-in, fast, TypeScript-native)
- **Test DB:** Separate PostgreSQL instance via Docker Compose (`db-test` service)
- **Mocks:** Telegram API responses (grammY test utilities), Gemini API responses
- **Factories:** Builder functions for test data (`createTestUser()`, `createTestChat()`, etc.)
- **Fixtures:** Pre-seeded DB states for common scenarios

---

## 16. Deployment

### 16.1 Docker Compose

```yaml
services:
  bot:
    build: .
    container_name: derp-bot
    env_file: .env.prod
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:17-alpine
    container_name: derp-db
    environment:
      POSTGRES_USER: derp
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: derp
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U derp"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  db-test:
    image: postgres:17-alpine
    container_name: derp-db-test
    environment:
      POSTGRES_USER: derp_test
      POSTGRES_PASSWORD: test
      POSTGRES_DB: derp_test
    ports:
      - "5433:5432"
    profiles: [test]

volumes:
  pgdata:
```

### 16.2 Dockerfile

```dockerfile
FROM oven/bun:1 AS base
WORKDIR /app

# Install system dependencies (ffmpeg for media conversion)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install JS dependencies
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production

# Copy source
COPY src/ src/
COPY drizzle/ drizzle/
COPY drizzle.config.ts tsconfig.json ./

CMD ["bun", "run", "src/index.ts"]
```

### 16.3 Startup Sequence

1. Parse and validate configuration (Zod) — crash on invalid config
2. Initialize OpenTelemetry SDK
3. Connect to PostgreSQL via Drizzle
4. Run pending migrations (`drizzle-kit push` or `migrate()`)
5. Create grammY `Bot` instance
6. Register middleware stack (in order)
7. Register tool registry → command handlers (with aliases) + LLM tool schemas
8. Call `setMyCommands` per locale (auto-generated from registry)
9. Initialize reminder scheduler (load due reminders, fire overdue ones)
10. Start long-polling via `grammY runner` (with error handling & auto-reconnect)
11. Log startup complete with environment info

### 16.4 Graceful Shutdown

```typescript
process.on('SIGTERM', async () => {
  logger.info('shutdown_initiated')
  runner.stop()                    // Stop accepting updates
  await scheduler.stop()           // Stop reminder scheduler
  await runner.task()              // Wait for in-flight handlers
  await db.end()                   // Close DB pool
  await otel.shutdown()            // Flush telemetry
  process.exit(0)
})
```

---

## 17. Code Scalability Principles

The codebase is designed to scale to 50+ tools and many handlers without architectural changes. Key patterns:

### 17.1 Build Once, Use Everywhere

| Module | Built once | Used by |
|--------|-----------|---------|
| `common/reply.ts` | Message splitting, formatting, balance footer, media sending | Every handler, every tool |
| `tools/credit-gate.ts` | Credit check → execute → deduct wrapper | Every paid tool |
| `tools/registry.ts` | Tool → command handler + LLM schema + help entry + pricing | Every tool definition |
| `middleware/hydrator.ts` | User/chat/message/member upsert | Every incoming update |
| `middleware/session.ts` | Credit balance, tier, subscription status | Every handler |
| `llm/context-builder.ts` | Participant registry + message stream | Every LLM call |
| `common/ffmpeg.ts` | Format conversion (WAV→OGG, WebM→MP4, etc.) | TTS, stickers, audio |

### 17.2 Adding a New Tool

Adding a tool requires **one file** in `src/tools/`:

```typescript
// src/tools/translate.ts
import { defineTool } from './registry'

export default defineTool({
  name: 'translate',
  commands: ['/translate', '/tr'],
  description: 'Translate text to another language',
  helpText: 'help-translate',
  category: 'utility',
  parameters: z.object({ text: z.string(), targetLang: z.string() }),
  credits: 3,
  freeDaily: 5,
  capability: 'TEXT',

  async execute(params, ctx) {
    // Implementation here
    return { text: translatedText }
  },
})
```

No wiring needed. The registry auto-discovers tool files, registers commands (including aliases), generates LLM function schemas, and adds the tool to `/help`.

### 17.3 grammY Plugin Composition

Plugins are composed as transformers (outgoing) and middleware (incoming), keeping handler code clean:

```typescript
// bot.ts — all infrastructure is plugin-based
bot.api.config.use(autoRetry())           // Retry 429/5xx
bot.api.config.use(apiThrottler())        // Respect Telegram flood limits

bot.use(hydrateReply)                     // ctx.replyFmt() with safe formatting
bot.use(autoChatAction())                 // Auto "typing..." while handlers run
bot.use(limit({ timeFrame: 2000, limit: 3 }))  // Per-user rate limit
bot.use(i18n)                             // Per-user locale
bot.use(conversations())                  // Multi-step flows
bot.use(settingsMenu)                     // Interactive menus
```

Handlers never deal with retry logic, throttling, rate limiting, or typing indicators. They just do business logic.

---

## 18. Project Structure

```
derp/
├── src/
│   ├── index.ts                     # Entry point: startup sequence
│   ├── config.ts                    # Zod-validated env config
│   ├── bot.ts                       # Bot instance, middleware, router registration
│   │
│   ├── handlers/                    # grammY composers (one per feature)
│   │   ├── start.ts                # /start onboarding (private + group)
│   │   ├── help.ts                 # /help (auto-generated from registry)
│   │   ├── chat.ts                 # Main agent handler (mention/reply/DM)
│   │   ├── settings.ts            # /settings (Menu plugin + conversations)
│   │   ├── credits.ts             # /credits, /buy, payment callbacks, transfers
│   │   ├── reminders.ts           # /remind, /reminders
│   │   ├── inline.ts              # Inline mode (placeholder → edit)
│   │   ├── info.ts                # /info (reply to bot message → metadata)
│   │   └── admin.ts               # /admin * commands
│   │
│   ├── middleware/                   # grammY middleware
│   │   ├── error-boundary.ts      # Global error handler
│   │   ├── logger.ts              # Structured logging
│   │   ├── hydrator.ts            # Upsert user/chat/message/member to DB
│   │   ├── session.ts             # Load credits, tier, inject services
│   │   └── rate-limiter.ts        # Per-user throttle
│   │
│   ├── tools/                       # Tool definitions (one file per tool)
│   │   ├── registry.ts            # ToolRegistry class
│   │   ├── types.ts               # ToolDefinition, ToolContext, ToolResult
│   │   ├── credit-gate.ts         # Credit check + deduct wrapper
│   │   ├── web-search.ts          # Brave Search (primary) + DuckDuckGo (fallback)
│   │   ├── imagine.ts             # Image generation
│   │   ├── edit-image.ts          # Image editing
│   │   ├── video.ts               # Video generation (Veo)
│   │   ├── tts.ts                 # Text-to-speech
│   │   ├── think.ts               # Deep reasoning
│   │   ├── remind.ts              # Create/list/cancel reminders
│   │   ├── memory.ts              # Chat memory update
│   │   └── get-member.ts          # Get chat member profile photo
│   │
│   ├── llm/                         # LLM provider abstraction
│   │   ├── types.ts               # LLMProvider interface, ChatParams, etc.
│   │   ├── registry.ts            # Model registry (tiers, capabilities, pricing)
│   │   ├── context-builder.ts     # Compact context window (participants + messages)
│   │   ├── prompt.ts              # System prompt templates (personality presets)
│   │   └── providers/
│   │       └── google.ts          # Google GenAI SDK implementation
│   │
│   ├── credits/                     # Credit economy
│   │   ├── service.ts             # CreditService (check, deduct, purchase, transfer)
│   │   ├── types.ts               # ModelTier, CreditCheckResult, etc.
│   │   ├── packs.ts               # Top-up pack definitions with decoy analysis
│   │   ├── subscriptions.ts       # Subscription plan definitions
│   │   └── ui.ts                  # /buy keyboard builders (subs first, packs second)
│   │
│   ├── scheduler/                   # Reminder scheduler
│   │   ├── scheduler.ts           # Main scheduler loop (pg-backed)
│   │   └── executor.ts            # Reminder execution (plain text or LLM call)
│   │
│   ├── db/                          # Database layer
│   │   ├── schema.ts              # Drizzle table definitions (single source of truth)
│   │   ├── connection.ts          # Pool creation, lifecycle
│   │   ├── queries/               # Query functions by domain
│   │   │   ├── users.ts
│   │   │   ├── chats.ts
│   │   │   ├── messages.ts
│   │   │   ├── credits.ts
│   │   │   ├── reminders.ts
│   │   │   └── members.ts
│   │   └── migrations/            # Generated by drizzle-kit
│   │
│   ├── common/                      # Shared utilities
│   │   ├── extractor.ts           # Media extraction from Telegram messages
│   │   ├── ffmpeg.ts              # ffmpeg wrapper (WAV→OGG, WebM→MP4, etc.)
│   │   ├── sanitize.ts            # MarkdownV2 escaping, text cleanup
│   │   ├── telegram.ts            # Formatting helpers, user display names
│   │   └── reply.ts               # Reply composition (text, image, multi-message, balance footer)
│   │
│   └── i18n/                        # Internationalization
│       ├── index.ts               # i18n middleware setup
│       └── locales/
│           ├── en.ftl             # English
│           └── ru.ftl             # Russian
│
├── tests/
│   ├── unit/                       # Mirrors src/ structure
│   ├── integration/
│   ├── e2e/
│   └── fixtures/                   # Shared test data factories
│
├── drizzle.config.ts                # Drizzle Kit config
├── Dockerfile
├── docker-compose.yml
├── package.json
├── tsconfig.json
├── biome.json                       # Linter + formatter
├── env.example
├── AGENTS.md                        # AI agent coding guidelines
└── README.md
```

---

## 19. Migration Plan

### 18.1 Database

The TypeScript rewrite connects to a fresh PostgreSQL database. The Drizzle schema defines all tables from scratch. User data from the Python version is migrated via a one-time script:

1. Export users, chats, messages, credit_transactions from old DB
2. Transform to new schema (rename columns, add new fields, generate UUIDs)
3. Import into new DB
4. Verify data integrity (credit balances, message counts)

### 18.2 Bot Token Cutover

1. Stop the Python bot
2. Run Drizzle migrations on new DB (or push schema)
3. Start the TypeScript bot with the same token
4. Verify via `/admin status`
5. Monitor for 24h, roll back if needed (restart Python bot)

### 18.3 Feature Parity Checklist

- [ ] Chat responses (mention, reply, DM, `/derp`)
- [ ] Name mention trigger (`derp`, `дерп`)
- [ ] Media extraction (photos, videos, audio, documents, stickers) with ffmpeg
- [ ] Image generation (`/imagine`, `/i`)
- [ ] Image editing (`/edit`, `/e`)
- [ ] Video generation (`/video`, `/v`)
- [ ] Text-to-speech (`/tts`)
- [ ] Deep thinking (`/think`, `/t`)
- [ ] Web search (`/search`, `/s`, agent tool)
- [ ] Chat memory (`/memory`, `/memory_set`, `/memory_clear`, agent tool)
- [ ] Inline mode (placeholder → edit pattern)
- [ ] Settings menu (`/settings` with Menu plugin, personality presets, permissions)
- [ ] Group onboarding (permissions setup on bot join)
- [ ] Credit system (tiers, deduction, balance display with low-balance warning at 20)
- [ ] Subscriptions (Lite/Pro/Ultra via Telegram Stars)
- [ ] Top-up credit packs (decoy-priced)
- [ ] Credit transfer (personal → group, min 100)
- [ ] Refunds
- [ ] Reminders (`/remind`, `/reminders`, plain + LLM modes)
- [ ] Message introspection (`/info`)
- [ ] Bot admin commands (`/admin *`)
- [ ] Onboarding (`/start` private, group intro)
- [ ] Auto-generated help (`/help`)
- [ ] Auto-registered commands with aliases (`setMyCommands` per locale)
- [ ] Forum topics support (`thread_id` scoping)
- [ ] i18n (English + Russian)
- [ ] Structured logging (OpenTelemetry)
- [ ] Reaction tracking
- [ ] Bot message metadata (model, tokens, tools, credits, duration)
- [ ] Participant tracking (optimistic, cached getChat/getChatMember)
- [ ] Edited message handling
- [ ] Graceful shutdown

---

## 20. Non-Goals (Out of Scope)

- **Multi-platform** — Telegram only. No Discord, Slack, WhatsApp.
- **Webhook mode** — Long-polling only. Simpler, no public URL needed.
- **Web dashboard** — All interaction through Telegram.
- **Multiple bot instances** — Single-tenant, single bot token.
- **Message search** — Messages stored for context, not user-queryable.
- **File storage / CDN** — Media is ephemeral (Telegram hosts files).
- **OpenAI / Anthropic / OpenRouter** — Google-only at launch. Contracts exist for future providers.
- **Voice input transcription** — Gemini handles audio natively, no separate STT step.
- **Per-member memory** — Memory is per-chat only. Per-user preferences may come later.

---

## 21. Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | **Runtime** | Bun | Native TS, fast startup, built-in test runner. |
| 2 | **ORM** | Drizzle | Schema-as-code, `drizzle-kit push` for auto-migrations. |
| 3 | **LLM Provider** | Google only (with contracts) | Gemini covers all capabilities. Interfaces allow future providers. |
| 4 | **Monetization** | Subscriptions (target) + top-up credits (decoy) | Subscriptions drive recurring revenue. Packs priced as decoys. |
| 5 | **Context format** | Compact participant registry + message stream | 70% token savings vs JSON. Maximizes Gemini cache hits. |
| 6 | **Chat memory limit** | 4096 chars | More room for persistent context. Enforced in app. |
| 7 | **Memory/reminder access** | Configurable per chat (admins / everyone) | Friends group → everyone. Public group → admins only. Set in onboarding. |
| 8 | **Inline mode** | Placeholder → edit (deferred generation) | Avoids wasting LLM calls on every keystroke. |
| 9 | **Reminders** | V1 launch, plain + LLM modes | Plain reminders are free. LLM reminders use tools. |
| 10 | **Settings UI** | grammY Menu plugin + conversations | Menus for toggles, conversations for text input. |
| 11 | **Admin naming** | `/admin *` prefix, `BOT_ADMIN_IDS` env var | Clear separation from Telegram chat admins. |
| 12 | **Table naming** | `ledger`, `usage_quotas`, `FREE` tier | Precise domain language. |
| 13 | **Telegram updates** | Reactions + member changes + edited messages | Feedback, participant tracking, content freshness. |
| 14 | **Tool-command duality** | Single definition → commands (with aliases) + LLM tool + help | One file per tool. `/imagine` and `/i` both work. |
| 15 | **Balance UX** | Show after every deduction, warn at 20 credits absolute | Users always know balance. 20 credits = ~2 images or 1 think+TTS. |
| 16 | **Bot message tracking** | Store metadata (model, tokens, tools, cost) on outgoing messages | Enables `/info`, transparency, cost analysis. |
| 17 | **Participant scoping** | Only include context-active participants | Prevents 500-member participant blocks in large groups. |
| 18 | **Commands** | Underscored (`/memory_set`) for clickability | `/memory set` sends only `/memory` when clicked. |
| 19 | **Security** | No chatId/userId in tool parameters | Prevents prompt injection cross-chat data access. |
| 20 | **Media processing** | ffmpeg system dependency in Docker | Required for TTS (WAV→OGG), stickers (WebM→MP4), audio normalization. |
| 21 | **Personality** | 4 presets + custom (subscribers only) | Covers common use cases. Custom allows full override for power users. |
| 22 | **Pricing anchor** | $0.013/Star developer payout | All margins calculated against actual revenue, not user purchase price. |
| 23 | **Web search** | Brave Search API primary, DuckDuckGo fallback | Brave has free tier with API key. DuckDuckGo unreliable at scale (IP bans). |
| 24 | **Welcome bonus** | 25 free credits on first `/start` | Trial-to-paid funnel. Users experience STANDARD tier immediately. Idempotent (never granted twice). |
| 25 | **Tool call limit** | 5 per request | Covers complex multi-tool flows with headroom. Prevents runaway chains. |
| 26 | **Message splitting** | Shared `reply` module, split on paragraph/sentence | 4096 char Telegram limit. Single implementation used by all handlers. |
| 27 | **Group privacy** | Must be disabled in BotFather | Required for context window to see all group messages. Documented in setup. |
| 28 | **grammY plugins** | parse-mode, auto-retry, throttler, ratelimiter, runner, menu, conversations, auto-chat-action | Don't rebuild what exists. Compose via transformers and middleware. |
| 29 | **Scalability** | One file per tool, auto-discovery, shared infrastructure | Adding a tool = one file. No wiring. Registry handles everything. |
