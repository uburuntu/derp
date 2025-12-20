# Repository Guidelines

## Project Structure & Module Organization

- `derp/`: Main package (entrypoint `__main__.py`).
- `derp/handlers/`, `derp/middlewares/`, `derp/filters/`: Telegram logic split by concern. Add new handlers under `derp/handlers/` and include their router in `derp/__main__.py`.
- `derp/common/`: Shared services (utils, LLM integration, Telegram helpers).
- `derp/db/`: Database module (session management, queries).
- `derp/models/`: SQLAlchemy models (User, Chat, Message).
- `derp/locales/`: i18n sources (`.po/.pot`) and compiled `.mo` files.
- `tests/`: Pytest suite (async-friendly, real database integration).
- `migrations/`: Alembic migrations (generated via `make db-revision`).

## Build, Test, and Development Commands

Always run commands instead of creating generated files manually. If Docker/database is unavailable, leave a TODO or ask the user to run the command.

- Bootstrap env: `make venv` (creates `.venv` and syncs deps quietly)
- Install deps: `make install` (runs `uv sync`)
- Run bot locally: `make run`
- Lint: `make lint` (Ruff)
- Format: `make format` (Ruff format)
- Tests: `make test` (quick, no DB) or `make test-all` (with PostgreSQL)
- Database tests: `make test-db` (requires Docker)
- Coverage: `make test-cov` (generates HTML report)
- i18n: `make i18n` (extract → update → compile)
  - Subcommands: `make i18n-extract`, `make i18n-update`, `make i18n-compile`
  - Init new locale: `make i18n-init LOCALE=fr`
- Database migrations:
  - Generate: `make db-revision MSG="add new table"` (**never create manually**)
  - Apply: `make db-migrate`
  - Status: `make db-status`
  - Rollback: `make db-downgrade`
- Docker: `make docker-up` (build/start) and `make docker-down` (stop)
- Development setup: `make dev-setup` (venv + db + migrations)
- Help: `make help` (lists available targets)

## Coding Style & Naming Conventions

- Python 3.13+, 4‑space indentation, type hints required.
- Naming: modules/functions `snake_case`, classes `CamelCase`, constants `UPPER_SNAKE`.
- Imports: prefer absolute within `derp.*`.
- Keep handlers small; place cross‑cutting logic in `middlewares/` or `common/`.
- Lint/format before pushing; CI runs Ruff check and format validation.
- Prefer concise idiomatic Python: walrus operator (`if x := getattr(obj, "attr", None):`), comprehensions with conditionals, `or` for defaults.

## Code Quality Principles

- **Reader-first:** Make intent obvious; enable local reasoning; minimize accidental complexity.
- **Illegal states unrepresentable:** Use types and invariants in data models.
- **Effects at edges:** Keep core logic pure; side effects in handlers/middlewares.
- **Sharp interfaces:** Small, cohesive modules with clear pre/post-conditions.
- **Fail fast:** Explicit errors for programmer mistakes; degrade gracefully for environmental failures.
- **Observability built-in:** Log decision points with structured key-value pairs.
- **Abstractions earned:** Prefer duplication over premature abstraction; extract after 3+ repetitions.

### Key Patterns

```python
# Guard clauses over pyramids
def load_user(uid: str) -> User:
    if not uid: raise ValueError("uid required")
    user = repo.get(uid)
    if user is None: raise NotFound(uid)
    return user

# Functional core, imperative shell
def price_after_discounts(items: list[Item]) -> Money:  # pure
    return sum(apply_discounts(i) for i in items)

# Command-Query separation
def calculate_quote(cart: Cart) -> Money  # query
def submit_order(cart_id: str) -> OrderId  # command

# Structured logging
logfire.info("checkout", cart_id=cart_id, total=total.amount)
```

### Exception & Logging Best Practices

- **Never log error then raise:** Let exceptions propagate; the final handler logs.
- **Warning for recoverable, then fallback:** Log warning, then use fallback value.
- **`logfire.exception()` only at boundaries:** Use in top-level handlers where you catch-all and reply with a friendly message.
- **Include traceback with `_exc_info=True`:** For `.warning()` and `.error()` inside `except` blocks, add `_exc_info=True` to capture the traceback. `.exception()` does this automatically.
- **Include context in logs:** Always add identifiers as structured attributes for traceability.
- **Fail fast internally, degrade gracefully externally:** Raise for programmer errors; recover for environmental failures.

```python
# BAD: double-logging
except SomeError as exc:
    logfire.exception("operation_failed")  # logged here
    raise  # ...and logged again by caller

# GOOD: warning + fallback
except SomeError as exc:
    logfire.warning("operation_failed_fallback")
    return fallback_value

# GOOD: let it propagate, log at boundary
async def handler(...):
    try:
        await do_work()
    except Exception:
        logfire.exception("handler_failed")  # only logged here
        await message.reply("Something went wrong")
```

### Smells to Avoid

- Deep nesting → guard clauses
- "Util" god-modules → per-domain modules
- Dicts as objects → Pydantic/`dataclass`/`TypedDict`
- Boolean args → explicit functions or `Enum`
- Silent exception swallowing → log + re-raise
- Logging error before raising → double-logging noise
- Context-specific comments → code should be self-explanatory; comments explain *why*, not *what*
- Hardcoded field maps → use `hasattr()` for duck typing when fields are mutually exclusive (e.g., `text` vs `caption` in Telegram methods)
- Scattered error handling → centralize in middleware when the same fallback applies everywhere (e.g., HTML parse errors handled in `ResilientRequestMiddleware`)

## Testing Guidelines

- Frameworks: `pytest`, `pytest-asyncio`.
- Name tests `tests/test_*.py`; use async tests for coroutine code.
- Database tests use real PostgreSQL via Docker (`make test-db`).
- Use `db_session` fixture for tests with automatic transaction rollback.
- Use factory fixtures (`user_factory`, `chat_factory`, `message_factory`) to create test data.
- Prefer reusable fixtures in `conftest.py` over duplicating mocks across test files.
- Aim to cover filters, handlers' pure logic, database queries, and utilities.
- Run locally with `uv run pytest -v` or `make test`.

## Commit & Pull Request Guidelines

- Use Conventional Commits where possible: `feat:`, `fix:`, `chore:`, `refactor:`, etc. Example: `fix: streamline reply handling`.
- PRs: include what/why, linked issues, and screenshots/log snippets if behavior changes.
- Requirements: passing CI (Ruff + tests), updated docs/i18n/migrations when applicable.
- PR description should briefly justify key design trade-offs, list known limitations, and explain how to delete or extend the change later.

## Security & Configuration

- Do not commit secrets. Use `env.example` to populate `.env`/`.env.prod`.
- Required env: Telegram token, `DATABASE_URL`, OpenAI/Google/OpenRouter keys, `LOGFIRE_TOKEN`, `ENVIRONMENT`.
- Production containers run non‑root; prefer read‑only FS and minimal privileges.

## Architecture Overview

Note: this section is descriptive, not prescriptive. It reflects the current implementation and is not set in stone. If requirements change, evolve the architecture.

- **Runtime Core:** `aiogram` v3 with a single `Dispatcher` and in‑memory FSM storage. Entry is `derp/__main__.py` which wires logging, i18n, DB client, middlewares, and routers, then starts long‑polling.
- **Update Flow:** Telegram Update → outer middlewares (logging + DB) → filters → inner middlewares (context + chat settings + chat actions) → matched handler router.
- **Concerns Split:**
  - `derp/handlers/*`: message/inline/media logic.
  - `derp/middlewares/*`: cross‑cutting concerns (logging, DB persistence, event context, DB model injection, credit service, throttling helper).
  - `derp/filters/*`: input shaping (mentions, meta command/hashtag parser).
  - `derp/common/*`: shared services (LLM, extraction, executors, Telegram helpers).
  - `derp/credits/*`: credit economy (pricing, tiers, service, registries).
  - `derp/db/*`: database session and query functions.
  - `derp/models/*`: SQLAlchemy models (User, Chat, Message, CreditTransaction, DailyUsage).
  - `derp/tools/*`: LLM tool implementations (chat memory, web search, image gen, think).
  - `derp/llm/*`: LLM provider abstraction and agent factories.
  - `derp/locales/*`: i18n resources and compiled catalogs.

## Event Handling & Middlewares

- **Routers:** Registered in `derp/__main__.py` via `dp.include_routers(...)` in this order: `debug` (admin only), `basic`, `donations`, `chat_settings`, `credit_cmds`, `payments`, `image`, `inline`, then catch‑all `chat` last.
- **Outer middlewares:**
  - `LogUpdatesMiddleware`: formats and logs each `Update` with elapsed ms.
  - `DatabaseLoggerMiddleware`: upserts user/chat and projects messages to the messages table.
- **Inner middlewares:**
  - `EventContextMiddleware`: injects `bot`, `db`, and derived `user`, `chat`, `thread_id`, `business_connection_id` into handler `data`. Note: `user` and `chat` here are aiogram types (with `.id` for Telegram ID).
  - `DatabaseModelMiddleware`: loads SQLAlchemy models from DB and injects `user_model` (`UserModel`) and `chat_model` (`ChatModel`) into handler `data`. These have `.telegram_id` for the Telegram ID and `.id` for the database UUID.
  - `CreditServiceMiddleware`: creates a `CreditService` instance with a fresh DB session and injects it as `credit_service` into handler `data`.
  - `ChatActionMiddleware`: shows typing/upload actions for long‑running handlers.
  - `ThrottleUsersMiddleware` (available): prevents concurrent handling per user; not enabled by default.
- **Session middlewares:**
  - `ResilientRequestMiddleware`: handles transient Telegram API errors at the session level, including retry on `TelegramRetryAfter` (flood control) and automatic fallback to plain text on HTML parse errors (`can't parse entities`). This means handlers don't need to catch these errors individually.

## LLM Integration (Pydantic-AI)

- **Provider Abstraction:** `derp/llm/providers.py` defines `ModelTier` enum and `create_model()` factory. Models are selected by tier (CHEAP, STANDARD, PREMIUM, IMAGE), not by name, enabling easy provider switching.
- **Agent Factories:** `derp/llm/agents.py` provides `create_chat_agent()`, `create_image_agent()`, `create_inline_agent()` pre-configured with system prompts and toolsets.
- **Dependencies:** `AgentDeps` dataclass (`derp/llm/deps.py`) injects context (message, chat, user, db, bot, tier, credit_service) into tools and prompts.
- **Result Wrapper:** `AgentResult` (`derp/llm/result.py`) standardizes agent output and provides `reply_to()` for sending Telegram messages with text, images, code blocks.
- **Handlers:**
  - `derp/handlers/chat.py`: main chat handler. Determines tier from credits, builds context, runs agent, handles multi-modal output.
  - `derp/handlers/image.py`: premium image generation/editing via `/imagine` and `/edit` commands.
  - `derp/handlers/inline.py`: inline mode with placeholder-then-edit pattern.
- **Tools & Toolsets:**
  - `derp/tools/toolsets.py`: creates `FunctionToolset` instances with registered tools (chat memory, web search, image gen, think).
  - Tools wrapped with `credit_aware_tool` for access control and credit deduction.
  - Chat memory stored in `chats.llm_memory` column, capped at 1024 chars.

### Pydantic-AI Tool Best Practices

**Naming:** `snake_case`, descriptive (`generate_image` not `gen_img`). File names reflect provider (`gemini_image.py`, `veo_video.py`).

**Docstrings:** Google style. First line = tool description for model. `Args:` = parameter descriptions (omit `ctx`).

**Return values:** Strings only. Direct-sending tools return `"[Sent directly to chat. Do not output anything else unless the user asked a follow-up question.]"`.

**Limits:** `UsageLimits(tool_calls_limit=3)` on agent runs to prevent abuse.

**Parameters:** Simple types (`str`, `int`, `bool`). Use `| None` for optionals.

## Data & Persistence (PostgreSQL + SQLAlchemy)

- **Session Management:** `derp/db/session.py` provides `DatabaseManager` with async session context managers.
- **Models:** `derp/models/` contains SQLAlchemy 2.0 models:
  - `User`: Telegram users with computed `full_name`, `display_name` properties.
  - `Chat`: Chats with `llm_memory` for LLM context, computed `display_name`.
  - `Message`: Conversation history for LLM context building, with `is_deleted` property.
- **Queries:** `derp/db/queries.py` contains typed async query functions:
  - `upsert_user`, `upsert_chat`, `upsert_message`: idempotent creates/updates.
  - `get_recent_messages`: returns messages in chronological order for LLM context.
  - `update_chat_memory`: sets/clears chat memory.
- **Migrations:** Alembic migrations in `migrations/versions/`. Generate with `make db-revision MSG="..."`.

## Credit Economy

The bot uses a credit-based monetization system with tiered access to features.

### Core Concepts

- **Two Credit Pools:** Users have personal credits; chats (groups) have shared pool credits. Chat credits are consumed first, then personal credits.
- **Model Tiers:** LLM models are abstracted into quality tiers (CHEAP, STANDARD, PREMIUM, IMAGE) rather than specific model names. This allows swapping providers without changing business logic.
- **Free Tier:** Users without credits use the CHEAP tier with reduced context length and no premium tools.
- **Paid Tier:** Users/chats with credits > 0 unlock STANDARD tier, longer context, and premium tools.

### Architecture

```
derp/credits/
├── models.py     # ModelConfig, MODEL_REGISTRY, tier mappings
├── tools.py      # ToolConfig, TOOL_REGISTRY, tool pricing
├── types.py      # ModelTier, ModelType, TransactionType, CreditCheckResult
└── service.py    # CreditService: check access, deduct, purchase, refund
```

- **CreditService:** Central service for all credit operations. Accepts SQLAlchemy `UserModel`/`ChatModel` directly (not Telegram IDs). Performs atomic balance updates, records transactions with idempotency keys, and checks tool/model access. Injected via `CreditServiceMiddleware`.
- **Registries:** `MODEL_REGISTRY` and `TOOL_REGISTRY` define available models/tools with their costs. Pricing is derived from provider costs with a margin.
- **CreditCheckResult:** Returned by access checks; contains `allowed`, `reject_reason`, source (chat/user), and cost information.

### Payment Flow

1. User runs `/buy` or `/buy_chat` → shows inline keyboard with credit packs
2. User taps pack → `payments.py` creates Telegram Stars invoice via `bot.create_invoice_link()`
3. Telegram sends `pre_checkout_query` → bot approves
4. Telegram sends `successful_payment` → `CreditService.purchase_credits()` adds credits atomically
5. Transaction recorded with `telegram_charge_id` for idempotency and refund support

### Tool Credit Integration

- Tools are wrapped with `credit_aware_tool` decorator that checks access before execution
- Premium tools (image gen, deep thinking) are visible to the agent but return placeholder messages when credits are insufficient
- Daily usage limits tracked in `daily_usage` table for free-tier rate limiting

### Extending

- **Add a model:** Add entry to `MODEL_REGISTRY` with provider, tier, and pricing. Tests will fail if tier hierarchy is violated.
- **Add a tool:** Add entry to `TOOL_REGISTRY` with base cost and daily limits. Wrap function with `credit_aware_tool`.
- **Change pricing:** Update registry entries; credit costs are derived automatically from provider costs.

## Media & Extraction

- **Extractor:** `derp/common/extractor.py` supports photos (incl. image docs and static stickers), videos (incl. video stickers/animations/video notes), audio/voice, documents (PDF path supported), and text. Uses signed file URLs via `derp/common/tg.py` and `httpx` to download bytes.
- **Formatting:** Helpers in `derp/common/tg.py` format user/chat/message info and provide reply helpers for attachments.

## Filters & Commands

- **Derp mention:** `DerpMentionFilter` detects `derp|дерп` as whole words; covered by `tests/test_filter.py`.
- **MetaCommand:** `derp/filters/meta.py` parses both `/command` (with optional `@bot`) and `#hashtag_args` forms, returning a structured `MetaInfo` (keyword, args, target message/text) for handlers such as `/imagine` and `/edit`.

## Configuration & i18n

- **Settings:** `derp/config.py` uses `pydantic-settings` to load `.env` and `.env.prod`, with helpers for rotating Google API keys and deriving `bot_id`.
- **i18n:** `aiogram.utils.i18n` with catalogs under `derp/locales`. Use `make i18n` to extract/update/compile; `SimpleI18nMiddleware` installs runtime translation. Never manually edit `.mo` files—always generate them via `make i18n-compile`.

## Observability & Resilience

- **Logging/Tracing:** `logfire` is configured with service name and environment. In `dev`, instruments `httpx`. Also instruments system metrics, Pydantic failures, and Google GenAI calls. A `LogfireLoggingHandler` bridges stdlib logging.
- **Backpressure/Throttling:** `ThrottleUsersMiddleware` available to drop concurrent messages per user. For CPU/IO offload with timeouts, see `derp/common/executor.py` (thread/process pools with `ThrottlerSimultaneous` and per‑task timeouts).
- **Error Handling:** Handlers catch and log exceptions, replying with friendly fallbacks; image pipelines degrade to text if no images are returned.

### Instrumentation Guidelines

**Span placement:**
- Create spans at semantic boundaries: handler entry, LLM calls, DB queries, external I/O (media downloads).
- Do NOT span every function. If an operation is fast (<10ms) or has no decision points, skip it.
- Use `@logfire.instrument()` for standalone functions that warrant tracing; prefer explicit `with logfire.span(...)` in async contexts.

**Auto-instrumentation:**
- Gemini calls are auto-instrumented via `logfire.instrument_google_genai()`. Do not create manual `genai.generate` spans.
- Token usage (`gen_ai.usage.*`) and model details are captured automatically; avoid manual tracking.
- Metrics are aggregated within spans via `MetricsOptions(collect_in_spans=True)`.

**Structured attributes:**
- Use OpenTelemetry semantic conventions: `gen_ai.*`, `http.*`, `db.*`.
- Telegram context: `telegram.chat_id`, `telegram.user_id`, `telegram.message_id`.
- Business metrics: `derp.context_chars`, `derp.context_messages`, `derp.has_media`.
- Media operations: `media.type`, `media.file_size`, `media.downloaded_bytes`.
- Database operations: `db.operation`, `db.limit`, `db.rows_returned`.

**Log levels:**
- `debug`: Dev-only details (context sizes, cache hits). Filtered in production.
- `info`: Key events and successful operations. Default for spans.
- `warn`: Recoverable failures (media download failed, fallback used).
- `error`/`exception`: Failures requiring investigation.

**Anti-patterns:**
- Avoid logging inside tight loops.
- Don't log full message content at info level (use debug or omit).
- Don't create spans for synchronous, fast operations.
- Never log secrets, tokens, or API keys.
- Don't duplicate what auto-instrumentation already captures.

## Telegram/Aiogram Guidelines

- **Aiogram vs SQLAlchemy types:** aiogram `User`/`Chat` objects have `.id` for Telegram ID. SQLAlchemy `UserModel`/`ChatModel` have `.telegram_id` for Telegram ID and `.id` for database UUID. Middlewares inject both: `user`/`chat` (aiogram) and `user_model`/`chat_model` (SQLAlchemy). Pass SQLAlchemy models to `CreditService` and DB queries.
- Direct fields: aiogram types are Pydantic models; access fields directly (they exist and may be `None`), avoid `getattr(..., "field", None)` for defined attributes.
- Short-circuit idioms: prefer concise patterns for optionals like `user and user.id` and `user and user.username or ""`.
- Logging: instrument decision points with `logfire` and include identifiers (chat_id, user_id, payload) for traceability.
- Resilience: wrap network sends in try/except, degrade gracefully (e.g., fall back from media to text), and ensure auxiliary failures don't impact the core user flow.
- Comments: keep comments purposeful (document intent/invariants); avoid restating obvious behavior that the code already conveys.

## Major Libraries

When generating code, setting up configuration, or needing API documentation for any of these libraries, use the Context7 MCP tools (`resolve-library-id` and `get-library-docs`) automatically to get up-to-date references.

- **aiogram 3.x:** Telegram bot framework (routers, middleware, filters, FSM, i18n).
- **pydantic-ai:** Provider-agnostic LLM framework (agents, tools, structured output, multi-provider support).
- **SQLAlchemy 2.x + asyncpg:** Async PostgreSQL ORM with typed models.
- **Alembic:** Database migrations.
- **logfire 4.x:** Structured logging, metrics, instrumentation.
- **pydantic 2.x + pydantic-settings:** Config and validation.
- **throttler:** Concurrency control for executors.
- **aiocache, aiojobs:** Caching and background job utilities (available; enable as needed).
- **babel/pybabel:** i18n extraction/update/compile.
- **dev tools:** `uv`, `ruff`, `pytest`, `pytest-asyncio`.

## Extending the Bot

- **Add a handler:**
  - Create `derp/handlers/<name>.py` with a `Router` and handlers.
  - Import and add the router in `derp/__main__.py` via `dp.include_routers(...)` in the right order.
- **Add a middleware:** Implement `BaseMiddleware` (or `UserContextMiddleware`) in `derp/middlewares/` and register as outer or inner depending on concern.
- **Add a filter:** Place in `derp/filters/` and use in router decorators.
- **Add a database migration:** Run `make db-revision MSG="description"` (**never create migration files manually**).
- **Add a model:** Create in `derp/models/`, add to `derp/models/__init__.py`, generate migration.
- **Add a query:** Add function to `derp/db/queries.py`, add tests in `tests/test_db_queries.py`.
- **Add a tool for LLM:** Write a function with `RunContext[AgentDeps]` as first param; add to `derp/tools/`, register in toolset, wrap with `credit_aware_tool` if it costs credits.
- **Add a credit pack:** Add entry to `CREDIT_PACKS` in `payments.py`.
- **Add a debug command:** Add to `derp/handlers/debug.py` (admin-only filter is already applied).

---

## Maintaining This Document

When updating AGENTS.md:
- **Integrate, don't prepend.** New information should be added to the appropriate existing section, not stacked at the top.
- **Critical Rules** are reserved for hard constraints (things that break the build or corrupt state if violated).
- **Keep sections cohesive.** If a new topic doesn't fit anywhere, consider whether it's substantial enough to warrant its own section or can be folded into an existing one.
- **Prefer brevity.** One-liners in the right place beat a new paragraph at the top.
- **"Remember" = AGENTS.md.** When the user says "remember this" or similar, always add the information to AGENTS.md in the appropriate section.
