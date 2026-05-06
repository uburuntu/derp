# Derp Architecture

Derp is layered around explicit product instruments. Each layer owns one boundary and talks downward through stable contracts.

## Layers

- `src/index.ts` composes runtime startup: config, database, bot, health, scheduler, shutdown.
- `src/bot`, `src/middleware`, and `src/handlers` are Telegram interface code. They translate grammY updates into application calls and user-facing replies. Large command surfaces can split into focused helper modules such as `admin-metrics` and `admin-refunds`.
- `src/tools/<instrument>.ts` files are product instruments. A tool owns its parameter schema, command parsing, help/pricing metadata, and execution logic. It never imports grammY, bot context, handlers, the registry, or credit gate internals.
- `ToolDefinition.execute` receives `ToolContext`, not the credit service. Spend checks, reservations, refunds, and receipts belong to `src/tools/credit-gate.ts`.
- `ToolContext` exposes narrow stores such as `memoryStore` and `reminderStore`, not a raw database handle. Instruments can mutate their own product surface but cannot make arbitrary persistence calls.
- Tool-facing `user` and `chat` objects are narrow identities, not full database rows. Add fields deliberately when an instrument genuinely needs them.
- `src/tools/registry.ts` is the Telegram/LLM adapter for instruments. It registers slash commands, confirmation callbacks, and help sections, then delegates execution runtime concerns.
- `src/tools/runtime/*` owns Telegram execution context construction, outgoing tool delivery, ledger delivery status, and message persistence. Tool instruments receive these capabilities through `ToolContext`.
- `src/tools/catalog.ts` and `src/tools/schema.ts` own tool presentation metadata and Zod-to-LLM schema/command parsing. The registry consumes these helpers instead of owning catalog rules.
- `src/tools/credit-gate.ts` is the application spend boundary. Instruments do not charge directly.
- `src/credits`, `src/preferences`, `src/memory`, `src/scheduler/cron.ts`, and `src/llm/registry.ts` hold product policy and domain rules.
- `src/db` owns persistence: schema and query modules only. Credit balance mutation lives in `credit-ledger`, free-use reservation lives in `credit-usage`, and payment/refund settlement remains behind `credits`.
- `src/llm/providers` owns external AI API adapters. Providers report accounting through `ProviderCallRecorder`; they do not import the database.
- `src/llm/providers/runtime.ts` owns generic provider plumbing such as retry, timeout, bounded downloads, and key rotation so vendor adapters stay focused on request/response translation.
- `src/platform` wires infrastructure ports and cross-cutting Telegram policy to implementations. `createDbProviderCallRecorder` is the DB-backed provider accounting adapter; `telegram-access` owns reusable chat-admin and group-chat decisions.
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
