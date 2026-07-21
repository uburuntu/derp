# Repository Guidelines

## Project Structure & Module Organization

- `derp/`: Main package. `__main__.py` owns process observability/bootstrap;
  `application.py` owns runtime resources, middleware, routers, and workers.
- `derp/handlers/`, `derp/middlewares/`, `derp/filters/`: Telegram boundaries.
  Register new routers in `derp/application.py::APPLICATION_ROUTERS`.
- `derp/common/`: Shared Telegram rendering and small cross-domain helpers.
- `derp/history/`, `derp/media/`: Scoped conversation state and bounded source
  media hydration.
- `derp/catalog/`, `derp/execution.py`, `derp/features/`: Canonical provider
  facts, validated plans/outcomes, and provider-neutral feature services.
- `derp/operations/`, `derp/approvals/`, `derp/artifacts/`, `derp/delivery/`:
  Paid-operation accounting, deferred consent, and durable result delivery.
- `derp/operator/`: Identifier-free aggregate diagnostics, fail-closed operator
  access, short-lived confirmations, and adapters to live maintenance workers.
- `derp/billing/`: Versioned Stars products, intents, settlement, subscriptions,
  refunds, and clawbacks.
- `derp/db/`, `derp/models/`: Cohesive persistence stores and SQLAlchemy models;
  Alembic migrations are the schema authority.
- `derp/locales/`: i18n sources (`.po/.pot`) and compiled `.mo` files.
- `tests/`: Pytest suite (async-friendly, real database integration).
- `migrations/`: Alembic migrations (generated via `make db-revision`).
- `docs/architecture-roadmap.md`: Normative product contract plus a dated,
  evidence-based implementation-status ledger.
- `docs/message-style.md`: Required English/Russian voice, terminology, message
  order, and review checklist for every user-visible string.
- `docs/operator-console.md`: Deployment-operator access, privacy boundaries,
  maintenance semantics, test controls, and extension/removal guidance.
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

`docs/architecture-roadmap.md` is the normative product and architecture
contract. Its dated status ledger distinguishes shipped behavior, intentionally
suspended surfaces, and external activation gates. Delete a legacy path only
when its replacement is covered and working or the public surface fails closed.

- **Runtime Core:** `derp/application.py` owns the aiogram dispatcher and every
  closeable runtime resource through one `AsyncExitStack`; `derp/__main__.py`
  owns process observability and the application boundary.
- **Update Flow:** Telegram update -> content-free tracing and source-event
  persistence -> event context -> router match -> route-scoped model/commerce
  dependencies -> handler.
- **Concerns Split:**
  - `derp/handlers/*`: message/inline/media logic.
  - `derp/middlewares/*`: cross‑cutting concerns (logging, DB persistence, event context, DB model injection, credit service, throttling helper).
  - `derp/filters/*`: input shaping (mentions, meta command/hashtag parser).
  - `derp/common/*`: shared services (LLM, extraction, executors, Telegram helpers).
  - `derp/catalog/*`: immutable provider model facts and pricing.
  - `derp/execution.py`: validated feature/model plans and typed outcomes.
  - `derp/operations/*`: immutable quotes, wallet reservations, settlement,
    request binding, telemetry, and startup reconciliation.
  - `derp/approvals/*`: authenticated durable deferred-tool approvals.
  - `derp/delivery/*`, `derp/artifacts/*`: durable Telegram delivery and
    private TTL-bound generated artifacts.
  - `derp/billing/*`: versioned Stars products, purchase intents,
    subscriptions, settlement, and clawbacks.
  - `derp/operator/*`: private deployment-control policy, aggregate snapshots,
    confirmation capabilities, and serialized live-worker maintenance.
  - `derp/credits/*`: transitional free-tool and legacy compatibility policy.
  - `derp/features/*`: provider-neutral chat, image, TTS, video, thinking, and
    inline application services.
  - `derp/db/*`: database session and query functions.
  - `derp/models/*`: normalized history and policy, wallet lots/events/consent,
    quotes and operations, approvals, artifacts and delivery intents, billing,
    shared facts, and inline allowances.
  - `derp/tools/*`: LLM tool implementations exposed through governed toolsets.
  - `derp/llm/*`: LLM provider abstraction and agent factories.
  - `derp/locales/*`: i18n resources and compiled catalogs.

## Event Handling & Middlewares

- **Routers:** `operator`, `operator_rejection`, `debug`, `debug_rejection`,
  `context_settings`, `basic`, `donations`, `credit_cmds`,
  `premium_suspension`, `payments`, `subscriptions`, `paid_media_delivery`,
  `image`, `tts`, `inline`, then catch-all `chat`. Privileged rejection routers
  consume unauthorized or stale controls before conversation handling.
- **Outer middlewares:**
  - `LogUpdatesMiddleware`: formats and logs each `Update` with elapsed ms.
  - `DatabaseLoggerMiddleware`: upserts user/chat and projects messages to the messages table.
- **Update middleware:** `EventContextMiddleware` injects static runtime and
  aiogram context for every update. `RouteDependencyMiddleware` runs after a
  handler matches and loads SQLAlchemy models or commerce services only for
  routes listed in `ROUTE_DEPENDENCY_PLANS`; legacy credit injection is not a
  global update cost. Operator routes use dispatcher-injected console services
  without model loading; only the subsequent durable debug-purchase callback
  loads models and purchase-intent services.
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
- **Agent Factories:** `derp/llm/agents.py` provides `create_chat_agent()`,
  `create_image_agent()`, and `create_inline_agent()`. They accept validated
  `ExecutionPlan` objects (or resolve a compatible default plan). Chat tools are
  attached per run through `create_chat_toolset()`.
- **Dependencies:** `AgentDeps` dataclass (`derp/llm/deps.py`) injects context
  (message, chat, user, db, bot, exact model spec) into tools and prompts.
- **Result Wrapper:** `AgentResult` (`derp/llm/result.py`) standardizes agent output and provides `reply_to()` for sending Telegram messages with text, images, code blocks.
- **Handlers:**
  - `derp/handlers/chat.py`: quotes the standard context band, atomically
    authorizes one provider run, falls back to the bounded economy plan when
    appropriate, and captures only an acknowledged model-content delivery.
    Failed delivery releases spend; ordinary chat text is not yet persisted for
    post-crash resend.
  - `derp/handlers/image.py` and `tool_approvals.py`: command and natural image
    requests converge on one approval, operation, feature, and delivery path.
  - `derp/handlers/tts.py`: exact quote -> authenticated approval -> bounded
    TTS -> durable voice artifact -> capture/delivery/reversal.
  - `derp/handlers/inline.py`: free economy answers admitted by an atomic
    per-user UTC-day allowance with strict request/token/output limits.
  - `premium_suspension.py`: `/think` and `/video` fail closed with no provider
    call or charge until their new services receive complete adapters.
- **Tools & Toolsets:**
  - `derp/tools/toolsets.py`: creates policy-derived `FunctionToolset` instances.
    Suspended premium capabilities are excluded even from manually assembled
    access values.
  - Only web search uses the transitional `credit_aware_tool` wrapper. Image
    tools require a server-approved deferred call and the operation ledger.
  - Natural TTS, thinking, and video tools remain absent until they can preserve
    the same quote, approval, delivery, and parent-turn settlement guarantees.

### Pydantic-AI Tool Best Practices

**Naming:** `snake_case`, descriptive (`generate_image` not `gen_img`). Provider
adapters use explicit names such as `image_executor.py` and `tts_executor.py`.

**Docstrings:** Google style. First line = tool description for model. `Args:` = parameter descriptions (omit `ctx`).

**Return values:** Provider executors return typed domain outcomes and never
send Telegram messages or mutate balances. Thin command/tool adapters translate
outcomes; do not add another direct-sending implementation.

**Limits:** `UsageLimits(tool_calls_limit=3)` on agent runs to prevent abuse.

**Parameters:** Simple types (`str`, `int`, `bool`). Use `| None` for optionals.

## Data & Persistence (PostgreSQL + SQLAlchemy)

- **Session Management:** `derp/db/session.py` provides `DatabaseManager`; a
  database transaction never spans provider or Telegram I/O.
- **Domain Stores:** history/context queries live under `derp/db/`; operation,
  approval, artifact, delivery, billing, wallet, and inline stores stay with
  their cohesive subsystems and exchange typed domain values.
- **Models:** SQLAlchemy models cover users/chats, normalized history and policy,
  wallet lots/events/consent, quotes and paid operations, deferred approvals,
  artifacts and delivery attempts, purchase intents/receipts/subscriptions,
  shared facts, and inline daily allowances.
- **Migrations:** Alembic is authoritative. Generate migrations with
  `make db-revision MSG="..."`; parity tests must fail on model/schema drift.

## Credit Economy

The bot uses a credit-based monetization system with tiered access to features.

### Core Concepts

- **Wallets:** A personal wallet contains expiring subscription allowance and
  purchased credits; a group wallet contains purchased shared credits. One
  operation uses one wallet, tries the chat first, and requires authenticated
  per-user/per-chat consent before personal fallback.
- **Model Keys:** Stable semantic keys select immutable Google model specs. The
  shared spec carries the exact provider ID, lifecycle, limits, capabilities,
  source links, and current pricing used by both execution and billing.
- **Free Tier:** An unfunded ordinary chat turn gets one idempotently claimed
  economy run. Inline chat has its own atomic per-user UTC-day allowance.
- **Paid Tier:** Users/chats with credits unlock the standard chat role, longer
  context, and premium tools.

### Architecture

```
derp/catalog/
└── google.py     # Immutable Google model specs, limits, capabilities, pricing
derp/operations/
├── quotes.py          # Fixed catalog-derived quotes and context bands
├── ledger.py          # Wallet selection, reserve/capture/release/reversal
├── bindings.py        # HMAC request identities without stored content
└── reconciliation.py  # Crash-boundary cleanup without provider replay
derp/billing/
├── products.py        # Immutable top-ups and one recurring plan
├── intents.py         # Opaque, expiring, payer/target-bound purchase intents
└── settlement.py      # Idempotent fulfillment, cycles, refunds, clawbacks
```

- **OperationLedger:** The authoritative paid-operation state machine. Database
  transactions cover state only; provider and Telegram I/O occur outside them.
- **QuoteEngine:** Derives a fixed quote from the same canonical
  `ExecutionPlan` used at runtime. Pricing inputs contain bounded commercial
  values and keyed request/delivery bindings, never prompt content.
- **Legacy CreditService:** Retained only for remaining compatibility and free
  web-search accounting. New provider-backed work must not use it.

### Payment Flow

- Public `/buy` and `/buy_chat` intake is controlled by
  `PUBLIC_PURCHASES_ENABLED` and defaults closed until the real Stars smoke test
  is recorded. Reconciliation remains live even while intake is closed.
- Invoice creation persists an opaque expiring intent before Telegram I/O.
  Pre-checkout validates payer, target, product version, currency, amount, and
  expiry; successful payment fulfills exactly once by charge ID.
- Subscription renewals create non-rolling 30-day allowance cycles. Cancellation
  changes future renewal only; refunds claw back the exact purchase/cycle source
  and record debt rather than making spendable inventory negative.
- Donation billing is independent and remains before credit purchase routes.

### Tool Credit Integration

- Paid chat, image, and TTS use immutable operation IDs and atomic settlement.
- Only premium tools backed by durable approval/accounting are visible to the
  agent. Thinking and video are absent from toolsets and intercepted commands.
- Free web-search usage remains in `daily_usage`; inline use has the separate
  concurrency-safe `inline_daily_allowances` table.

### Extending

- **Add a model:** Update the single Google catalog and its drift tests; runtime
  resolution and verified provider pricing must change together.
- **Add a paid tool:** Reuse the quote/operation service and typed feature
  outcome. The legacy `TOOL_REGISTRY` and `credit_aware_tool` path is
  transitional, not a pattern to duplicate.
- **Change pricing:** Verify current provider pricing, then update the single
  catalog and quote tests. Existing TODO prices are not authoritative.

## Media & Extraction

- **Source media:** History stores normalized Telegram attachment references,
  never bytes or signed URLs. `MediaGateway` uses one owned HTTP client and
  hydrates references on demand with time, size, MIME, and redirect limits.
- **Generated media:** `DeliveryService` persists bounded private artifacts,
  records intent before Telegram sends, distinguishes definite from ambiguous
  failure, authenticates no-charge resend, and reverses captured spend on
  terminal failure or expiry.
- **Formatting:** `derp/common/tg.py` and `MessageSender` own Telegram rendering;
  provider executors have no Telegram dependency.

## Filters & Commands

- **Derp mention:** `DerpMentionFilter` detects `derp|дерп` as whole words; covered by `tests/test_filter.py`.
- **MetaCommand:** `derp/filters/meta.py` parses both `/command` (with optional `@bot`) and `#hashtag_args` forms, returning a structured `MetaInfo` (keyword, args, target message/text) for handlers such as `/imagine` and `/edit`.

## Configuration & i18n

- **Settings:** `derp/config.py` uses `pydantic-settings` to load `.env` and `.env.prod`, with helpers for rotating Google API keys and deriving `bot_id`.
- **Operator:** A deployment operator is an explicit `OPERATOR_IDS` allowlist
  member, distinct from Telegram chat administrators and owners. Production
  requires at least one ID; `ADMIN_IDS` is a deprecated migration alias.
- **i18n:** `aiogram.utils.i18n` with catalogs under `derp/locales`. Use `make i18n` to extract/update/compile; `SimpleI18nMiddleware` installs runtime translation. Never manually edit `.mo` files—always generate them via `make i18n-compile`.
- **Message style:** Follow `docs/message-style.md` for all fixed copy. English
  is concise and conversational; Russian is tighter, uses `Дерп`, and must not
  retain fuzzy or untranslated production entries.
- **Command menu:** `derp/command_menu.py` is the desired state for private,
  group, and group-admin command scopes. Startup reapplies every supported
  locale and deletes the default scope so stale BotFather commands cannot expose
  suspended features.

## Observability & Resilience

- **Logging/Tracing:** `derp/observability.py` owns Logfire configuration, scrubbing, integrations, stdlib logging, and shutdown. `derp/application.py` owns the bot/database runtime. Every update gets one content-free `telegram.update` consumer span.
- **Operator console:** `docs/operator-console.md` defines its aggregate-only
  data contract, conservative maintenance results, and `operator.*` events.
- **Backpressure/Throttling:** Polling has a configurable global concurrency limit. `ThrottleUsersMiddleware` is available for per-user exclusion but is not enabled.
- **Error Handling:** Boundaries report privacy-safe failures once and return a
  typed not-charged, refunded, retry, or reconciliation state. Paid media never
  substitutes generic text and then treats the requested artifact as delivered.

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

- **Aiogram vs SQLAlchemy types:** aiogram `User`/`Chat` objects have `.id` for
  Telegram ID. SQLAlchemy models have `.telegram_id` and a database UUID `.id`.
  Event context provides aiogram values globally; SQLAlchemy models are present
  only when the matched route dependency plan requests them.
- Direct fields: aiogram types are Pydantic models; access fields directly (they exist and may be `None`), avoid `getattr(..., "field", None)` for defined attributes.
- Short-circuit idioms: prefer concise patterns for optionals like `user and user.id` and `user and user.username or ""`.
- Logging: instrument decision points with `logfire` and include safe identifiers and outcomes; never include Telegram or payment payload content.
- Personal balances, subscription state, payment outcomes, and refund amounts are
  actor-only. In groups, use `deliver_sensitive_reply()` for a protected private
  message plus a content-free public acknowledgement; callbacks that mutate a
  personal plan must fail closed outside the private chat.
- Operator controls are additionally allowlist-gated and private-chat-bound.
  Never infer operator access from Telegram chat administrator/owner status.
- Resilience: recover network sends only where the product contract permits.
  Paid artifact failure uses durable delivery/reversal state rather than a
  generic text substitute; auxiliary failures must not corrupt the core flow.
- Comments: keep comments purposeful (document intent/invariants); avoid restating obvious behavior that the code already conveys.

## Major Libraries

When generating code, setting up configuration, or needing API documentation,
use the `ctx7` CLI workflow defined at the top of this file. For aiogram and
pydantic-ai, inspect the lock-matched `references/` checkout as the primary
source for repository-specific changes.

- **aiogram 3.30.0:** Telegram runtime. Consult `references/aiogram` before changing routers, middleware, dependency injection, polling, flags, payments, or session middleware.
- **pydantic-ai 2.14.1:** Agent runtime. Consult its upgrade guide and `references/pydantic-ai` before changing agents, capabilities, tools, history, retries, instrumentation, or durable execution.
- **SQLAlchemy 2.x + asyncpg:** Async PostgreSQL ORM with typed models.
- **Alembic:** Database migrations.
- **logfire 4.38.0:** Structured logging, metrics, instrumentation. Consult `references/logfire` before changing configuration, integrations, scrubbing, tracing, metrics, or exporter lifecycle.
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
- **Add a credit pack:** Add an immutable versioned product to
  `DEFAULT_PRODUCT_CATALOG`; both intent creation and fulfillment consume the
  persisted version and amount.
- **Add an operator control:** Extend identifier-free types and aggregates under
  `derp/operator/`, then add bounded presentation in
  `derp/handlers/operator.py`. Reuse a live bounded domain worker for mutations,
  require an actor/action-bound confirmation, update router dependencies and
  history exclusions, and preserve fail-closed stale-control handling. Do not
  add generic debug mutation commands; `/debug_buy` is the compatibility path
  for the durable 1-Star validation flow.

---

## Maintaining This Document

When updating AGENTS.md:
- **Integrate, don't prepend.** New information should be added to the appropriate existing section, not stacked at the top.
- **Critical Rules** are reserved for hard constraints (things that break the build or corrupt state if violated).
- **Keep sections cohesive.** If a new topic doesn't fit anywhere, consider whether it's substantial enough to warrant its own section or can be folded into an existing one.
- **Prefer brevity.** One-liners in the right place beat a new paragraph at the top.
- **"Remember" = AGENTS.md.** When the user says "remember this" or similar, always add the information to AGENTS.md in the appropriate section.
