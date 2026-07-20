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
- `docs/architecture-roadmap.md`: Cross-cutting work deferred from chores.
- `references/`: Gitignored upstream source checkouts matching the lockfile.

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

- Python 3.14+, 4‑space indentation, type hints required.
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
- **Privacy-safe exception reports only at boundaries:** Use `report_exception()` in top-level handlers that recover and reply with a friendly message. It preserves safe stack locations while redacting exception text.
- **Never use raw exception capture:** `logfire.exception()` and `_exc_info` export exception messages and stack traces outside normal scrubbing. Let exceptions propagate or use `report_exception(..., level="warning" | "error")` when recovering.
- **Include context in logs:** Always add identifiers as structured attributes for traceability.
- **Fail fast internally, degrade gracefully externally:** Raise for programmer errors; recover for environmental failures.

```python
# BAD: double-logging
except SomeError as exc:
    report_exception("operation_failed", exception=exc)  # logged here
    raise  # ...and logged again by caller

# GOOD: warning + fallback
except SomeError as exc:
    report_exception("operation_failed_fallback", exception=exc, level="warning")
    return fallback_value

# GOOD: let it propagate, log at boundary
async def handler(...):
    try:
        await do_work()
    except Exception as exc:
        report_exception("handler_failed", exception=exc)  # only logged here
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
- Mark PostgreSQL tests with `pytest.mark.database`; `make test` excludes that
  marker and `make test-db` runs it.
- Test schemas come only from `alembic upgrade head`; never call
  `Base.metadata.create_all()` in fixtures.
- Use `db_session` for automatic outer-transaction rollback. Application code
  may call `commit()` because the session joins through `create_savepoint`.
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
- Required env: Telegram token, `DATABASE_URL`, `GOOGLE_API_PAID_KEY`, `LOGFIRE_TOKEN`, `ENVIRONMENT`.
- Production containers run non‑root; prefer read‑only FS and minimal privileges.

## Architecture Overview

Note: this section is descriptive, not prescriptive. It reflects the current implementation and is not set in stone. If requirements change, evolve the architecture.

`docs/architecture-roadmap.md` is the target product and architecture contract.
For cross-cutting work, follow its milestone order and delete each legacy path
only when its replacement is covered and working.

- **Runtime Core:** `aiogram` v3 with a single `Dispatcher` and in‑memory FSM storage. Entry is `derp/__main__.py` which wires logging, i18n, DB client, middlewares, and routers, then starts long‑polling.
- **Update Flow:** Telegram Update → outer middlewares (logging + DB) → filters → inner middlewares (context + chat settings + chat actions) → matched handler router.
- **Concerns Split:**
  - `derp/handlers/*`: message/inline/media logic.
  - `derp/middlewares/*`: cross‑cutting concerns (logging, DB persistence, event context, DB model injection, credit service, throttling helper).
  - `derp/filters/*`: input shaping (mentions, meta command/hashtag parser).
  - `derp/common/*`: shared services (LLM, extraction, executors, Telegram helpers).
  - `derp/catalog/*`: immutable provider model facts and pricing.
  - `derp/credits/*`: credit economy (feature policy, service, transactions).
  - `derp/db/*`: database session and query functions.
  - `derp/models/*`: SQLAlchemy models (User, Chat, Message, CreditTransaction, DailyUsage).
  - `derp/tools/*`: LLM tool implementations (chat memory, web search, image gen, think).
  - `derp/llm/*`: LLM provider abstraction and agent factories.
  - `derp/locales/*`: i18n resources and compiled catalogs.

## Event Handling & Middlewares

- **Routers:** Registered in `derp/__main__.py` in this order: `debug`, `basic`, `donations`, `chat_settings`, `credit_cmds`, `think`, `payments`, `image`, `video`, `tts`, `inline`, then catch-all `chat`.
- **Outer middlewares:**
  - `LogUpdatesMiddleware`: formats and logs each `Update` with elapsed ms.
  - `DatabaseLoggerMiddleware`: upserts user/chat and projects messages to the messages table.
- **Update middlewares** (run for every update before router dispatch):
  - `EventContextMiddleware`: injects `bot`, `db`, and derived `user`, `chat`, `thread_id`, `business_connection_id` into handler `data`. Note: `user` and `chat` here are aiogram types (with `.id` for Telegram ID).
  - `DatabaseModelMiddleware`: loads SQLAlchemy models from DB and injects `user_model` (`UserModel`) and `chat_model` (`ChatModel`) into handler `data`. These have `.telegram_id` for the Telegram ID and `.id` for the database UUID.
  - `CreditServiceMiddleware`: creates a `CreditService` instance with a fresh DB session and injects it as `credit_service` into handler `data`.
- **Event middlewares:**
  - `MessageSenderMiddleware`: injects `MessageSender` for messages and callback queries.
  - `ChatActionMiddleware`: shows typing/upload actions for long‑running handlers.
  - `ThrottleUsersMiddleware` (available): prevents concurrent handling per user; not enabled by default.
- **Session middlewares:**
  - `ResilientRequestMiddleware`: handles transient Telegram API errors at the session level, including retry on `TelegramRetryAfter` (flood control) and automatic fallback to plain text on HTML parse errors (`can't parse entities`). This means handlers don't need to catch these errors individually.

## LLM Integration (Pydantic-AI)

- **Provider Factory:** `derp/llm/providers.py` accepts an exact catalog spec or
  semantic key and creates the corresponding Google model. Provider switching
  is not currently implemented.
- **Agent Factories:** `derp/llm/agents.py` provides `create_chat_agent()`, `create_image_agent()`, and `create_inline_agent()`. Chat tools are attached per run through `create_chat_toolset()`.
- **Dependencies:** `AgentDeps` dataclass (`derp/llm/deps.py`) injects context
  (message, chat, user, db, bot, exact model spec) into tools and prompts.
- **Result Wrapper:** `AgentResult` (`derp/llm/result.py`) standardizes agent output and provides `reply_to()` for sending Telegram messages with text, images, code blocks.
- **Handlers:**
  - `derp/handlers/chat.py`: main chat handler. Resolves the catalog model from
    credit state, builds context, runs the agent, and handles multi-modal output.
  - `derp/handlers/image.py`: premium image generation/editing via `/imagine` and `/edit` commands.
  - `derp/handlers/inline.py`: inline mode with placeholder-then-edit pattern.
- **Tools & Toolsets:**
  - `derp/tools/toolsets.py`: creates `FunctionToolset` instances with registered tools (chat memory, web search, image gen, think).
  - Tools wrapped with `credit_aware_tool` for access control and credit deduction.
  - Chat memory stored in `chats.llm_memory` column, capped at 1024 chars.

### Pydantic-AI Tool Best Practices

**Naming:** `snake_case`, descriptive (`generate_image` not `gen_img`). File names reflect provider (`gemini_image.py`, `veo_video.py`).

**Docstrings:** Google style. First line = tool description for model. `Args:` = parameter descriptions (omit `ctx`).

**Return values:** Existing direct-sending tools use string sentinels for
compatibility. New or migrated feature executors return typed domain outcomes
and never send Telegram messages; thin command/tool adapters translate those
outcomes. Do not add another direct-sending implementation.

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

- **Two Credit Pools:** Users have personal credits; chats have shared credits.
  Current code checks chat then personal balances. The target requires explicit
  per-user, per-chat consent before personal fallback; never add silent fallback.
- **Model Keys:** Stable semantic keys select immutable Google model specs. The
  shared spec carries the exact provider ID, lifecycle, limits, capabilities,
  source links, and current pricing used by both execution and billing.
- **Free Tier:** Users without credits use the economy chat role with reduced
  context length and no paid-only tools.
- **Paid Tier:** Users/chats with credits unlock the standard chat role, longer
  context, and premium tools.

### Architecture

```
derp/catalog/
└── google.py     # Immutable Google model specs, limits, capabilities, pricing
derp/credits/
├── tools.py      # ToolConfig, TOOL_REGISTRY, tool pricing
├── types.py      # CreditCheckResult
├── service.py    # CreditService: check access, deduct, purchase, refund
└── purchase_suspension.py  # Fail-closed new-purchase boundary
```

- **CreditService:** Central service for all credit operations. Accepts SQLAlchemy `UserModel`/`ChatModel` directly (not Telegram IDs). Performs atomic balance updates, records transactions with idempotency keys, and checks tool/model access. Injected via `CreditServiceMiddleware`.
- **Catalogs:** `GOOGLE_MODEL_CATALOG` owns provider facts and pricing;
  `TOOL_REGISTRY` owns feature access policy and semantic model selection.
- **CreditCheckResult:** Returned by access checks; contains the exact immutable
  model spec, `allowed`, `reject_reason`, source (chat/user), and cost information.

### Payment Flow

- New credit purchases are intentionally suspended until the roadmap's durable
  purchase intents and end-to-end Stars validation ship. `/buy`, `/buy_chat`,
  legacy `buy:*`/`dbuy:*` callbacks, and credit pre-checkout queries must fail
  closed through `derp/credits/purchase_suspension.py`.
- Do not add an enable flag around the removed invoice code. Milestone 2 replaces
  the suspension boundary with a validated purchase-intent service.
- Successful-payment reconciliation remains registered for production and
  legacy debug payments so captured charges are fulfilled idempotently through
  `CreditService.purchase_credits()`.
- Donation invoice creation, pre-checkout approval, and fulfillment are
  independent and must stay before the catch-all credit rejection router.

### Tool Credit Integration

- Tools are wrapped with `credit_aware_tool` decorator that checks access before execution
- Premium tools (image gen, deep thinking) are visible to the agent but return placeholder messages when credits are insufficient
- Daily usage limits tracked in `daily_usage` table for free-tier rate limiting

### Extending

- **Add a model:** Do not add another mapping before the roadmap's unified
  catalog. During migration, runtime resolution and verified provider pricing
  must change together and remain covered by drift tests.
- **Add a paid tool:** Reuse the quote/operation service and typed feature
  outcome. The legacy `TOOL_REGISTRY` and `credit_aware_tool` path is
  transitional, not a pattern to duplicate.
- **Change pricing:** Verify current provider pricing, then update the single
  catalog and quote tests. Existing TODO prices are not authoritative.

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

- **Logging/Tracing:** `derp/observability.py` owns Logfire configuration, scrubbing, integrations, stdlib logging, and shutdown. `derp/application.py` owns the bot/database runtime. Every update gets one content-free `telegram.update` consumer span.
- **Backpressure/Throttling:** Polling has a configurable global concurrency limit. `ThrottleUsersMiddleware` is available for per-user exclusion but is not enabled.
- **Error Handling:** Handlers catch and log exceptions, replying with friendly fallbacks; image pipelines degrade to text if no images are returned.

### Instrumentation Guidelines

**Span placement:**
- Create spans at semantic boundaries: handler entry, LLM calls, DB queries, external I/O (media downloads).
- Do NOT span every function. If an operation is fast (<10ms) or has no decision points, skip it.
- Use `@logfire.instrument()` for standalone functions that warrant tracing; prefer explicit `with logfire.span(...)` in async contexts.

**Auto-instrumentation:**
- Configure Pydantic AI through the configured `Logfire.instrument_pydantic_ai(...)` instance so it uses the same tracer and meter providers. Content-free Google GenAI SDK child spans are intentional for direct and provider calls; do not add manual provider-generation spans.
- Run-level token usage is captured under `gen_ai.aggregated_usage.*`; provider
  spans may expose more specific `gen_ai.usage.*` attributes. Avoid duplicate
  manual token tracking.
- Metrics are aggregated within spans via `MetricsOptions(collect_in_spans=True)`.
- Direct Google SDK content and completion hooks are always disabled. The explicit local-dev opt-in enables only Pydantic AI text content and may never enable production or binary capture.
- The global exception callback must redact exception messages, stack source text, and status descriptions for auto-instrumented spans. Recovering boundaries use `report_exception()`; never bypass either layer.
- Do not globally instrument HTTPX: Telegram file URLs contain the bot token. Instrument only owned safe clients with headers and bodies disabled, or use explicit semantic spans.

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
- Never log message, prompt, query, callback, payment payload, tool arguments, or provider response content at any level; record lengths, types, identifiers, and outcomes instead.
- Don't create spans for synchronous, fast operations.
- Never log secrets, tokens, or API keys.
- Don't duplicate what auto-instrumentation already captures.
- Don't propagate Telegram identifiers through OpenTelemetry baggage. Local correlation belongs in the update root span and `UpdateContext`.

## Telegram/Aiogram Guidelines

- **Aiogram vs SQLAlchemy types:** aiogram `User`/`Chat` objects have `.id` for Telegram ID. SQLAlchemy `UserModel`/`ChatModel` have `.telegram_id` for Telegram ID and `.id` for database UUID. Middlewares inject both: `user`/`chat` (aiogram) and `user_model`/`chat_model` (SQLAlchemy). Pass SQLAlchemy models to `CreditService` and DB queries.
- Direct fields: aiogram types are Pydantic models; access fields directly (they exist and may be `None`), avoid `getattr(..., "field", None)` for defined attributes.
- Short-circuit idioms: prefer concise patterns for optionals like `user and user.id` and `user and user.username or ""`.
- Logging: instrument decision points with `logfire` and include safe identifiers and outcomes; never include Telegram or payment payload content.
- Resilience: wrap network sends in try/except, degrade gracefully (e.g., fall back from media to text), and ensure auxiliary failures don't impact the core user flow.
- Comments: keep comments purposeful (document intent/invariants); avoid restating obvious behavior that the code already conveys.

## Major Libraries

When generating code, setting up configuration, or needing API documentation,
use the `ctx7` CLI workflow defined at the top of this file. For aiogram and
pydantic-ai, inspect the lock-matched `references/` checkout as the primary
source for repository-specific changes.

- **aiogram 3.x:** Telegram runtime. Consult `references/aiogram` before changing routers, middleware, dependency injection, polling, flags, payments, or session middleware.
- **pydantic-ai 2.x:** Agent runtime. Consult its upgrade guide and `references/pydantic-ai` before changing agents, capabilities, tools, history, retries, instrumentation, or durable execution.
- **SQLAlchemy 2.x + asyncpg:** Async PostgreSQL ORM with typed models.
- **Alembic:** Database migrations.
- **logfire 4.x:** Structured logging, metrics, instrumentation. Consult `references/logfire` before changing configuration, integrations, scrubbing, tracing, metrics, or exporter lifecycle.
- **pydantic 2.x + pydantic-settings:** Config and validation.
- **babel/pybabel:** i18n extraction/update/compile.
- **dev tools:** `uv`, `ruff`, `pytest`, `pytest-asyncio`.

## Extending the Bot

- **Add a handler:**
  - Create `derp/handlers/<name>.py` with a `Router` and handlers.
  - Import and add the router in `derp/application.py` via `dispatcher.include_routers(...)` in the right order.
- **Add a middleware:** Implement `BaseMiddleware` (or `UserContextMiddleware`) in `derp/middlewares/` and register as outer or inner depending on concern.
- **Add a filter:** Place in `derp/filters/` and use in router decorators.
- **Add a database migration:** Run `make db-revision MSG="description"` (**never create migration files manually**).
- **Add a model:** Create in `derp/models/`, add to `derp/models/__init__.py`, generate migration.
- **Add a query:** Add function to `derp/db/queries.py`, add tests in `tests/test_db_queries.py`.
- **Add an LLM capability:** Build or reuse a feature service with typed outcomes,
  then expose it through a thin `RunContext[AgentDeps]` tool adapter and a
  role/policy-derived toolset. Do not add a second billing or direct-sending path.
- **Add a credit pack:** Wait for the roadmap's durable purchase-intent flow,
  then add an immutable, versioned pack consumed by invoice creation and
  fulfillment.
- **Add a debug command:** Add to `derp/handlers/debug.py` (admin-only filter is already applied).

---

## Maintaining This Document

When updating AGENTS.md:
- **Integrate, don't prepend.** New information should be added to the appropriate existing section, not stacked at the top.
- **Critical Rules** are reserved for hard constraints (things that break the build or corrupt state if violated).
- **Keep sections cohesive.** If a new topic doesn't fit anywhere, consider whether it's substantial enough to warrant its own section or can be folded into an existing one.
- **Prefer brevity.** One-liners in the right place beat a new paragraph at the top.
- **"Remember" = AGENTS.md.** When the user says "remember this" or similar, always add the information to AGENTS.md in the appropriate section.
