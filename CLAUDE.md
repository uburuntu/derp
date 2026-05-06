# Derp Bot — Agent Guidelines

## Project

Derp is a Telegram AI bot built with Bun + grammY + Drizzle ORM + Google GenAI SDK.

## Critical Rules

### 1. Always use Context7 for API lookups

**NEVER rely on your training data for Telegram Bot API or grammY API details.** Always query Context7:

- **Telegram Bot API docs:** Use library ID `/websites/core_telegram_bots_api` — for checking Telegram types (User, Chat, Message fields), API methods (sendMessage, sendInvoice, etc.), update types, payment flows, inline mode.
- **grammY docs:** Use library ID `/grammyjs/website` — for middleware patterns, plugin usage (menu, conversations, parse-mode, auto-retry, runner, i18n, ratelimiter, throttler, auto-chat-action), context flavoring, Bot API wrapper methods, error handling.

**Examples of when to use Context7:**
- Before accessing a field on a Telegram User/Chat/Message object → query Telegram Bot API docs
- Before using a grammY plugin → query grammY docs for correct import, setup, and usage
- Before calling any Bot API method → query both to confirm parameters
- Before handling payment/invoice flows → query Telegram Bot API for Stars/XTR specifics
- Before writing middleware → query grammY docs for middleware patterns and context flavoring

### 2. Use Bun, not Node.js

- `bun run <file>` not `node <file>`
- `bun test` not `jest`
- `bun install` not `npm install`
- `Bun.spawn` for subprocesses (ffmpeg)
- Bun auto-loads `.env` — no dotenv needed

### 3. PRD is the source of truth

If code, plan, or your training data conflicts with the PRD, the PRD wins.

### 4. Tier system

The orchestrator only returns `FREE` or `STANDARD`. Never `PREMIUM`. The `PREMIUM` tier exists in the model registry for cataloguing models like `gemini-3-pro-preview`, but is never returned by `CreditService.getOrchestratorConfig()`. Tools like `/think` use `defaultModel` to reference specific model IDs directly, bypassing tier-based selection.

### 5. Tool-command duality

Every tool is defined ONCE and produces: grammY command handlers (with aliases), LLM function schemas, `/help` entries, and pricing. Adding a tool = one file in `src/tools/`.

### 6. Security boundary

Tool functions NEVER receive `chatId` or `userId` as parameters. These are always derived from execution context. This prevents prompt injection cross-chat data access.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Runtime | Bun |
| Bot Framework | grammY |
| Database | PostgreSQL + Drizzle ORM |
| LLM | Google GenAI SDK (`@google/genai`) |
| Validation | Zod |
| Linter | Biome |
| Media Processing | ffmpeg (system dep) |
| Search | Brave Search API (primary), DuckDuckGo (fallback) |

## grammY Plugins

| Plugin | Package | Purpose |
|--------|---------|---------|
| parse-mode | `@grammyjs/parse-mode` | `fmt` template literals, `hydrateReply`, `ctx.replyFmt()` |
| auto-retry | `@grammyjs/auto-retry` | Retry on 429/5xx (API transformer) |
| throttler | `@grammyjs/transformer-throttler` | Outgoing flood control |
| ratelimiter | `@grammyjs/ratelimiter` | Incoming per-user rate limiting |
| menu | `@grammyjs/menu` | Interactive inline keyboards |
| conversations | `@grammyjs/conversations` | Multi-step interactions |
| runner | `@grammyjs/runner` | Concurrent long-polling |
| i18n | `@grammyjs/i18n` | Per-user locale, Fluent `.ftl` |
| auto-chat-action | `@grammyjs/auto-chat-action` | Auto "typing..." indicators |

## File Structure Conventions

- One tool per file in `src/tools/`
- One handler per file in `src/handlers/`
- One query module per file in `src/db/queries/`
- Shared infra in `src/common/` (reply, extractor, ffmpeg, sanitize, telegram, markdown, observability)
- All DB tables defined in `src/db/schema.ts` (single source of truth)

## Message Formatting Rules

All user-facing system messages (non-LLM) use **Telegram HTML** with `parse_mode: "HTML"`.

### Guidelines

1. **Use 1-2 emoji per message** as visual anchors — one at the title, optionally one for a secondary section. Don't overuse.
2. **Bold for section headers** — `<b>Section</b>`, not for individual words mid-sentence.
3. **Italic for hints/tips** — `<i>subtle guidance text</i>`.
4. **Keep messages scannable** — short lines, clear structure, whitespace between sections.
5. **Costs in italic** — `· <i>10 cr, 1 free/day</i>` at end of tool description.
6. **No raw MarkdownV2** — never use `parse_mode: "MarkdownV2"` anywhere. It breaks on 18 special characters.

### Formatting pipeline

| Source | Format | Sent as |
|--------|--------|---------|
| LLM responses | Standard Markdown (`**bold**`, `` `code` ``) | Converted via `markdownToHtml()` → `parse_mode: "HTML"` with plain text fallback |
| System messages (help, credits, info, start) | Telegram HTML directly (`<b>`, `<i>`, `<code>`) | `parse_mode: "HTML"` |
| Admin commands | Telegram HTML | `parse_mode: "HTML"` |
| Balance footer | Plain text with emoji | Appended to LLM response (goes through Markdown→HTML) |

### Example: well-formatted system message

```
🤖 <b>Derp</b>

🔍 <b>Search & Research</b>
  /search, /s — Search the web · <i>free</i>

🎨 <b>Creative</b>
  /imagine, /i — Generate images · <i>10 cr, 1 free/day</i>

<i>Just describe what you need — I'll pick the right tool.</i>
```
