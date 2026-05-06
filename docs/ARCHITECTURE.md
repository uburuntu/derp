# Derp Architecture

Derp is layered around explicit product instruments. Each layer owns one boundary and talks downward through stable contracts.

## Layers

- `src/index.ts` composes runtime startup: config, database, bot, health, scheduler, shutdown.
- `src/bot`, `src/middleware`, and `src/handlers` are Telegram interface code. They translate grammY updates into application calls and user-facing replies.
- `src/tools/<instrument>.ts` files are product instruments. A tool owns its parameter schema, command parsing, help/pricing metadata, and execution logic. It never imports grammY, bot context, handlers, the registry, or credit gate internals.
- `ToolDefinition.execute` receives `ToolContext`, not the credit service. Spend checks, reservations, refunds, and receipts belong to `src/tools/credit-gate.ts`.
- `src/tools/registry.ts` is the Telegram/LLM adapter for instruments. It registers slash commands, confirmation callbacks, help sections, persistence of outgoing messages, and command delivery.
- `src/tools/credit-gate.ts` is the application spend boundary. Instruments do not charge directly.
- `src/credits`, `src/preferences`, `src/memory`, `src/scheduler/cron.ts`, and `src/llm/registry.ts` hold product policy and domain rules.
- `src/db` owns persistence: schema and query modules only.
- `src/llm/providers` owns external AI API adapters. Providers report accounting through `ProviderCallRecorder`; they do not import the database.
- `src/platform` wires infrastructure ports to implementations. `createDbProviderCallRecorder` is the DB-backed provider accounting adapter.
- `src/common` contains context-free utilities: formatting, sanitization, money helpers, health, observability, media helpers.

## Instrument Boundaries

| Instrument | Owns | Must Not Own |
| --- | --- | --- |
| `web-search` | Query parsing, search provider calls, result formatting | Telegram delivery, quota charging |
| `think` | Paid reasoning prompt and model choice | Spend reservation, command confirmation |
| `imagine` | Image prompt execution and media result intent | Telegram file APIs, ledger writes |
| `edit-image` | Source-image requirement and edit execution | Telegram media extraction, command routing |
| `video` | Video generation workflow and billable provider failures | Delivery persistence, spend lifecycle |
| `tts` | Speech synthesis and audio conversion intent | Telegram voice upload details outside `ToolContext` |
| `memory` | Chat-memory mutation semantics | Raw Telegram user/chat IDs |
| `remind` | Reminder creation/list/cancel semantics | Scheduler polling or delivery execution |
| `get-member` | Scoped participant lookup via `p1`, `p2`, ... | Arbitrary Telegram user lookup |

These boundaries are enforced by `tests/unit/architecture.test.ts`.
