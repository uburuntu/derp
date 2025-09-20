# Repository Guidelines

## Project Structure & Module Organization

- `derp/`: Main package (entrypoint `__main__.py`).
- `derp/handlers/`, `derp/middlewares/`, `derp/filters/`: Telegram logic split by concern. Add new handlers under `derp/handlers/` and include their router in `derp/__main__.py`.
- `derp/common/`: Shared services (DB, utils, LLM integration).
- `derp/queries/`: Generated EdgeQL/Gel query helpers. Update via codegen when schema changes.
- `derp/locales/`: i18n sources (`.po/.pot`) and compiled `.mo` files.
- `tests/`: Pytest suite (async-friendly).
- `scripts/`: Utilities for i18n and Gel codegen.
- `dbschema/`: Gel database schema and migrations.

## Build, Test, and Development Commands

- Bootstrap env: `make venv` (creates `.venv` and syncs deps quietly)
- Install deps: `make install` (runs `uv sync`)
- Run bot locally: `make run`
- Lint: `make lint` (Ruff)
- Format: `make format` (Ruff format)
- Tests: `make test` (quiet) or `make test-verbose`
- i18n: `make i18n` (extract → update → compile)
  - Subcommands: `make i18n-extract`, `make i18n-update`, `make i18n-compile`
  - Init new locale: `make i18n-init LOCALE=fr`
- Gel codegen: `make gel-codegen`
- Docker: `make docker-up` (build/start) and `make docker-down` (stop)
- Help: `make help` (lists available targets)

## Coding Style & Naming Conventions

- Python 3.13+, 4‑space indentation, type hints required.
- Naming: modules/functions `snake_case`, classes `CamelCase`, constants `UPPER_SNAKE`.
- Imports: prefer absolute within `derp.*`.
- Keep handlers small; place cross‑cutting logic in `middlewares/` or `common/`.
- Lint/format before pushing; CI runs Ruff check and format validation.

## Code Quality Principles

- Implement the smallest coherent slice and optimize for safe change.
- Optimize for reader time: make intent obvious, minimize accidental complexity, and enable local reasoning.
- Use the team’s domain language for names; keep consistent conventions so style never competes with substance.
- Make illegal states unrepresentable via types and invariants in data models.
- Keep side effects at the edges; keep core logic pure and testable.
- Prefer small, cohesive, loosely coupled functions/modules with sharp interfaces and clear pre/post-conditions.
- Isolate boilerplate and hidden complexity behind sharp interfaces; document assumptions and invariants briefly in docstrings or comments.
- Errors: be explicit; fail fast on programmer mistakes, degrade gracefully on environmental failures.
- Observability is built-in: add appropriate logs/metrics/traces where behavior may surprise.
- Abstractions are earned (after real repetition), not speculative; prefer duplication over premature abstraction.
- Tests read like executable examples of behavior (not implementation details); measure performance instead of guessing.
- Code should be easy to change or delete tomorrow without fear.
- Avoid broad refactors without a concrete migration plan.

## Python Examples

Each maps to optimizing reader time, safe change, and hiding complexity at the edges.

1. Guard clauses (early returns) over pyramids — keep the happy path flat

```python
def load_user(uid: str) -> User:
    if not uid:
        raise ValueError("uid required")
    user = repo.get(uid)
    if user is None:
        raise NotFound(uid)
    return user
```

2. Functional core, imperative shell — pure logic inside, effects at boundaries

```python
def price_after_discounts(items: list[Item]) -> Money:  # pure
    return sum(apply_discounts(i) for i in items)

def checkout(cart_id: str, clock: Clock, payments: Payments):  # effects
    items = repo.load(cart_id)
    total = price_after_discounts(items)
    payments.charge(total, at=clock.now())
```

3. Make illegal states unrepresentable — types + invariants

```python
from dataclasses import dataclass
from typing import NewType, Literal
Currency = Literal["GBP","EUR","USD"]
UserId = NewType("UserId", str)

@dataclass(frozen=True)
class Money:
    amount: int  # cents
    currency: Currency
    def __post_init__(self):
        if self.amount < 0: raise ValueError("amount >= 0")
```

4. Protocol-based dependency inversion — depend on Protocols, not concrete classes

```python
from typing import Protocol

class Payments(Protocol):
    def charge(self, amount: Money, at: datetime) -> None: ...

def checkout(total: Money, payments: Payments):  # depends on protocol
    payments.charge(total, at=datetime.now(tz=UTC))
```

5. Command–Query separation — functions either do or calculate

```python
def calculate_quote(cart: Cart) -> Money  # query
def submit_order(cart_id: str) -> OrderId  # command
```

6. Tell, don’t ask — push logic into the type that owns the data

```python
@dataclass
class Subscription:
    expires_at: datetime
    def is_active(self, now: datetime) -> bool: return now < self.expires_at
```

7. Narrow, explicit errors; fail fast vs. degrade gracefully

```python
class NotFound(RuntimeError): pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential())
def fetch_profile(uid: UserId, http: Http) -> Profile: ...
```

8. Context managers for resource lifetimes — make setup/teardown obvious

```python
from contextlib import contextmanager
@contextmanager
def txn(db):
    try:
        db.begin(); yield
        db.commit()
    except:
        db.rollback(); raise
```

9. Structured logging at decision points — key-value over prose

```python
log.info("checkout", cart_id=cart_id, total=total.amount, currency=total.currency)
```

10. Prefer duplication over premature abstraction — extract after repetition

```python
# after 3 near-identical validators, extract:
def require(condition: bool, *, msg: str) -> None:
    if not condition: raise ValueError(msg)
```

11. Small modules, stable interfaces — hide helpers, export the surface

```python
__all__ = ["calculate_quote", "submit_order"]
```

12. Data pipelines as iterators/generators — stream, keep state local

```python
def read_csv(path: Path) -> Iterable[Row]:
    with path.open() as f:
        yield from csv.DictReader(f)

def transform(rows: Iterable[Row]) -> Iterable[Record]:
    for r in rows:
        if is_valid(r):
            yield normalize(r)
```

13. Type hints everywhere, but pragmatic — annotate boundaries meticulously

```python
def parse_iso(dt: str) -> datetime: ...
```

14. Idempotent, timeout-bounded I/O — safer retries and operability

```python
def put_event(evt: Event, *, timeout: float = 2.0, idempotency_key: str | None = None) -> None: ...
```

15. Caching with explicit invalidation — localize performance tweaks

```python
@lru_cache(maxsize=1024)
def country_by_ip(ip: str) -> str: ...
```

16. Pattern matching for discriminated unions — clearer branching

```python
match payment:
    case Card(number=_, cvv=_):
        ...
    case BankTransfer(iban=_):
        ...
```

17. Testing as executable spec — assert behavior, not calls

```python
def test_checkout_declines_on_expired_card(...): ...
```

## Smells To Stamp Out

- Deep nesting / long boolean expressions → prefer guard clauses, extract functions.
- “Util” god-modules / helpers catch-alls → define per-domain modules.
- Dictionaries as ad-hoc objects → use `dataclass`/`NamedTuple`/`TypedDict` with invariants.
- Boolean arguments (e.g., `send_email=True`) → prefer explicit functions or `Enum`.
- Implicit global state/singletons → inject dependencies; consider `contextvars` for request scope.
- Inheritance for reuse → prefer composition; reserve subclassing for real polymorphism.
- Silent exception swallowing → log with context; re-raise domain errors.

## Summary

- Use guard clauses; keep the happy path flat.
- Keep pure logic separate from side-effects; effects live at edges.
- Encode invariants with types; depend on Protocols; apply CQS and Tell/Don’t Ask.
- Handle errors with domain exceptions, timeouts, bounded retries; log decisions with structured logs.
- Extract abstractions only after repetition; keep interfaces sharp and small.
- Prefer iterators/generators for data flow; keep modules tiny with explicit exports.
- Write tests that read like examples; avoid mocking pure functions.

## Testing Guidelines

- Frameworks: `pytest`, `pytest-asyncio`.
- Name tests `tests/test_*.py`; use async tests for coroutine code.
- Aim to cover filters, handlers’ pure logic, and utilities; stub Telegram objects with `MagicMock`.
- Run locally with `uv run pytest -v`.

## Commit & Pull Request Guidelines

- Use Conventional Commits where possible: `feat:`, `fix:`, `chore:`, `refactor:`, etc. Example: `fix: streamline reply handling`.
- PRs: include what/why, linked issues, and screenshots/log snippets if behavior changes.
- Requirements: passing CI (Ruff + tests), updated docs/i18n/queries when applicable.
- PR description should briefly justify key design trade-offs, list known limitations, and explain how to delete or extend the change later.

## Security & Configuration

- Do not commit secrets. Use `env.example` to populate `.env`/`.env.prod`.
- Required env: Telegram token, Gel DSN/secret, OpenAI/Google/OpenRouter keys, `LOGFIRE_TOKEN`, `ENVIRONMENT`.
- Production containers run non‑root; prefer read‑only FS and minimal privileges.

## Architecture Overview

Note: this section is descriptive, not prescriptive. It reflects the current implementation and is not set in stone. If requirements change, evolve the architecture.

- **Runtime Core:** `aiogram` v3 with a single `Dispatcher` and in‑memory FSM storage. Entry is `derp/__main__.py` which wires logging, i18n, DB client, middlewares, and routers, then starts long‑polling.
- **Update Flow:** Telegram Update → outer middlewares (logging + DB) → filters → inner middlewares (context + chat settings + chat actions) → matched handler router.
- **Concerns Split:**
  - `derp/handlers/*`: message/inline/media logic.
  - `derp/middlewares/*`: cross‑cutting concerns (logging, DB persistence, event context, chat settings, throttling helper).
  - `derp/filters/*`: input shaping (mentions, meta command/hashtag parser).
  - `derp/common/*`: shared services (DB, LLM, extraction, executors, Telegram helpers).
  - `derp/queries/*`: Gel (EdgeDB) async query helpers (generated).
  - `derp/locales/*`: i18n resources and compiled catalogs.

## Event Handling & Middlewares

- **Routers:** Registered in `derp/__main__.py` via `dp.include_routers(...)` in this order: `basic`, `chat_settings`, `gemini_image`, `gemini_inline`, then catch‑all `gemini` last.
- **Outer middlewares:**
  - `LogUpdatesMiddleware`: formats and logs each `Update` with elapsed ms.
  - `DatabaseLoggerMiddleware`: asynchronously upserts user/chat + inserts BotUpdate in Gel on every update; marks `handled=True` if a handler processed it. Exposes `db_task` in `data` to avoid races when handlers also persist.
- **Inner middlewares:**
  - `EventContextMiddleware`: injects `bot`, `db`, and derived `user`, `chat`, `thread_id`, `business_connection_id` into handler `data`.
  - `ChatActionMiddleware`: shows typing/upload actions for long‑running handlers.
  - `ChatSettingsMiddleware`: loads per‑chat settings from Gel and adds `chat_settings` to `data`.
  - `ThrottleUsersMiddleware` (available): prevents concurrent handling per user; not enabled by default.

## LLM Integration (Gemini)

- **Client:** Native Google `genai` via `google-genai`. Wrapper in `derp/common/llm_gemini.py` provides a builder to compose:
  - text and media parts (`with_text`, `with_media`),
  - model selection (`with_model`),
  - optional tools (`with_tool`) with auto‑generated function declarations,
  - Google Search and URL Context (`with_google_search`, `with_url_context`).
- **Function Calling:** Responses with `function_calls` are iteratively executed by `_FunctionCallHandler` (max 3 hops), feeding results back into `generate_content`.
- **Result Shape:** `GeminiResult` extracts `text_parts`, `code_blocks`, `execution_results`, and inline `images` to simplify Telegram replies.
- **Handlers:**
  - `derp/handlers/gemini.py`: main chat handler. Triggers on `/derp`, private chats, replies to the bot, or `DerpMentionFilter`. Builds context from recent updates (`queries/select_active_updates_async_edgeql.py`) and optional chat memory, attaches media extracted by `derp/common/extractor.py`, executes Gemini, and replies with text/images. Stores bot responses back to DB.
  - `derp/handlers/gemini_image.py`: premium image generation/editing using `gemini-2.5-flash-image-preview`; supports media groups and caption fallback.
  - `derp/handlers/gemini_inline.py`: inline mode, returns placeholder first, then edits with model output.
- **Tools & Memory:**
  - Lightweight tool system in `derp/common/llm_gemini.py` for native Gemini function calling.
  - Chat memory update tools in `derp/tools/memory.py` (native wrapper) and `derp/tools/chat_memory.py` (PydanticAI style). The memory is stored in Gel via `update_chat_settings_async_edgeql.py` and capped at 1024 chars.

## Data & Persistence (Gel)

- **Client:** `derp/common/database.py` encapsulates a singleton `AsyncIOClient` with transaction and retry options. Use `get_executor()` to run queries.
- **Write Path:** `DatabaseLoggerMiddleware` calls `create_bot_update_with_upserts(... )` to upsert `telegram::User/Chat` and insert `BotUpdate` per update. After handler completion, `update_bot_update_handled_status` marks processed updates.
- **Queries:** Code‑generated async helpers live in `derp/queries/*_async_edgeql.py`. Regenerate with `make gel-codegen` when schemas in `dbschema/` change.

## Media & Extraction

- **Extractor:** `derp/common/extractor.py` supports photos (incl. image docs and static stickers), videos (incl. video stickers/animations/video notes), audio/voice, documents (PDF path supported), and text. Uses signed file URLs via `derp/common/tg.py` and `httpx` to download bytes.
- **Formatting:** Helpers in `derp/common/tg.py` format user/chat/message info and provide reply helpers for attachments.

## Filters & Commands

- **Derp mention:** `DerpMentionFilter` detects `derp|дерп` as whole words; covered by `tests/test_filter.py`.
- **MetaCommand:** `derp/filters/meta.py` parses both `/command` (with optional `@bot`) and `#hashtag_args` forms, returning a structured `MetaInfo` (keyword, args, target message/text) for handlers such as `/imagine` and `/edit`.

## Configuration & i18n

- **Settings:** `derp/config.py` uses `pydantic-settings` to load `.env` and `.env.prod`, with helpers for rotating Google API keys and deriving `bot_id`.
- **i18n:** `aiogram.utils.i18n` with catalogs under `derp/locales`. Use `make i18n` to extract/update/compile; `SimpleI18nMiddleware` installs runtime translation.

## Observability & Resilience

- **Logging/Tracing:** `logfire` is configured with service name and environment. In `dev`, instruments `httpx`. Also instruments system metrics and Pydantic failures. A `LogfireLoggingHandler` bridges stdlib logging.
- **Backpressure/Throttling:** `ThrottleUsersMiddleware` available to drop concurrent messages per user. For CPU/IO offload with timeouts, see `derp/common/executor.py` (thread/process pools with `ThrottlerSimultaneous` and per‑task timeouts).
- **Error Handling:** Handlers catch and log exceptions, replying with friendly fallbacks; image pipelines degrade to text if no images are returned.

## Major Libraries

- **aiogram 3.x:** Telegram bot framework (routers, middleware, filters, FSM, i18n).
- **google-genai:** Native Gemini client (generate content, tools, search, URL context).
- **gel 3.x:** EdgeDB/Gel async client with codegen helpers.
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
- **Add a Gel query:** Create/modify EdgeQL in `derp/queries/*.edgeql` or `dbschema/`, then run `make gel-codegen`.
- **Add a tool for Gemini:** Write a function with docstring and type hints; register via `GeminiRequestBuilder.with_tool(func, deps)` to expose it for function calls.
