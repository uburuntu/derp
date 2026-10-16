# Derp

- Derp is a fun, group-first Telegram bot: the sharp friend already in the chat. Quiet until invoked, aware of the obvious context, capable with text and images, honest about money.
- "It just works": interrupt users only when significant money is at stake (creator-sponsored usage or a paying user's balance). Model choice, charging and retries stay invisible.
- Persona copy is chaotic but brief; money, privacy and error copy stays plain. User-facing strings live in `derp/locales/` (en + ru).

## Stack and layout

- Python 3.14 + uv, aiogram 3 (long polling), Pydantic AI over OpenRouter, PostgreSQL via SQLAlchemy 2 (async) + Alembic, Logfire. Versions live in `pyproject.toml`/`uv.lock`; do not repeat them in docs.
- `derp/` is split into domain packages, each owning its models, queries and an `AGENTS.md`: `telegram/` (request policy, sending, filters), `inference/` (OpenRouter seam, model pins, provider-call ledger), `history/` (chat knowledge capture and query), `memory/` (notes, rules), `agent/` (runtime, persona, invocation, outputs), `money/` (wallets, metering, funding), `billing/` (Stars payments, subscriptions, refunds), `scheduler/`, `settings/` (chat/user policy UI), `tools/` (toolbox capabilities), `ops/` (observability, owner pings, preflight), `locales/`.
- `derp/app.py` composes services; imports never create clients or start work. `derp/config.py` is the only configuration authority.
- `migrations/` holds Alembic history; `tests/` mirrors `derp/`; `tools/` holds deployment and maintenance scripts.

## Commands

- `make install`, `make lint`, `make format`, `make typecheck`, `make test` (unit), `make test-db` / `make test-e2e` / `make test-all` (Docker PostgreSQL on 5433), `make run` (dev bot from `.env`).
- Migrations: `make revision MSG="..."`, review the generated file, never hand-write from scratch. After cutover migrations are expand-only (old and new code must both run against the new schema).

## Invariants

- Tools never accept `chat_id`/`user_id` parameters: scope always comes from the run context. Queries over chat data are always filtered by the run's chat.
- Money: balances never go negative; every movement is an append-only ledger entry keyed by an idempotent operation ID; capture only after Telegram accepted the output.
- Telemetry is content-free by default: numeric IDs, counts, durations and outcomes only — no message text, captions, tool arguments, file IDs, URLs or exception messages. Never instrument HTTPX globally (bot token is in file URLs).
- Never replay an ambiguous Telegram send. Guest-mode messages are never stored.
- Secrets come from the environment only, are `repr=False`, and never appear in logs, tests or commits.

## Working rules

- Verify Telegram, aiogram, Pydantic AI and OpenRouter behavior in `references/` (upstream checkouts + scraped docs, gitignored) instead of memory.
- Tests use synthetic data and never touch the network; `live`-marked tests are opt-in and spend real money. User-facing features land with an e2e journey through the fake Bot API (`tests/e2e/`).
- Keep modules small and direct; prefer a plain function to a new abstraction until the third repetition.
- A change people would notice (feature, command, payer rule, privacy) updates `derp/agent/guide.md` in the same change; Derp answers questions about itself from it. Numbers never go there: they are generated from settings in `agent/tools/about.py`.
- Every package directory has a short `AGENTS.md` (5–15 lines): what it owns, its invariants, where its contracts live. Update it in the same change that alters those facts. No session status, TODOs or progress logs in tracked docs.
