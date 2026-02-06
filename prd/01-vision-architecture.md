# Derp — Product Requirements Document (v2)

## Part 1: Vision, Architecture & Features

**Version:** 2.1
**Date:** 2026-02-06
**Status:** Draft

---

## 1. Product Overview

### 1.1 What is Derp?

Derp is an AI-powered Telegram agent that operates in the context of private and group chats. It has a large, extensible toolset — image generation, video generation, TTS, web search, deep reasoning, reminders, and more — with tools callable both by the user (via `/commands`) and autonomously by the LLM agent.

### 1.2 North Star

Build a **powerful agentic assistant** with a growing library of tools. The agent receives the full chat context and decides which tools to invoke — in parallel or sequentially — to fulfill user intent. The free tier exposes all tools visibly (with daily limits) so users experience the value before hitting a paywall.

**Agent-first examples:**

| User says | Agent does |
|-----------|-----------|
| "derp add a hat to @rm_bk's photo" | `getChatMember` → `getUserProfilePhotos` → download photo → `editImage` → reply |
| "derp summarize the chat" | Reads context window → synthesizes summary → reply |
| "derp explain the meme" | Extracts image from reply/thread → analyzes → reply |
| "derp find recent news about Trump" | `webSearch("Trump news today")` → synthesizes → reply |
| "derp remind me about this in 20 days" | `createReminder(message: "...", replyToMessageId: 4521, fireAt: now+20d)` → confirms |
| "derp post UK news every day at 9 AM" | `createReminder(prompt: "Search UK news and post summary", cron: "0 9 * * *", usesLlm: true)` → confirms |

Reminders support two modes:
- **Plain reminder:** stores a `message` string and sends it verbatim when the time comes. Can reference a `replyToMessageId` so the user remembers the context.
- **LLM reminder:** stores a `prompt` with `usesLlm: true`. When fired, the scheduler creates an agent call with tools (e.g., web search → summarize → post to chat).

### 1.3 Core Identity

- **Name:** Derp (`@DerpRobot`)
- **Platform:** Telegram (private chats, groups, supergroups)
- **Personality:** Helpful, conversational, naturally opinionated, adaptable, concise
- **Trigger:** Mention (`@DerpRobot`), name (`derp`, `дерп`), direct reply, private chat, or `/command`
- **Monetization:** Telegram Stars → subscriptions + credit top-ups

### 1.4 Design Principles

1. **Agent-first** — The LLM decides which tools to call. Commands are shortcuts, not the primary interface.
2. **Tools are universal** — Every tool is both a `/command` (with aliases) and an LLM function call. One definition, two interfaces.
3. **Free tier shows the product** — Free users see all tools (with daily limits). Premium tools return friendly "upgrade" messages, not silent failures.
4. **Flat-rate pricing** — Every tool has a fixed credit cost. No variable token-based surprises.
5. **Context is king** — Optimized, compact context window with participant registry and message stream for maximum LLM cache hits.
6. **Google-first, contract-ready** — Ship with Google (Gemini, Veo, TTS). Build against TypeScript interfaces so providers can be swapped later without business logic changes.

---

## 2. Target Users

| Segment | Description | Needs |
|---------|-------------|-------|
| **Casual Users** | Individuals who add the bot to private chat | Quick Q&A, web search, occasional image generation |
| **Group Members** | Members of Telegram groups where the bot is present | Shared AI assistant, entertainment, creative content |
| **Subscribers** | Users with Pro or Ultra subscriptions | Better model, higher context, premium tools, reminders |
| **Group Funders** | Any member who funds a group's credit pool | Upgrade the group experience for all members (not limited to admins) |
| **Bot Admins** | Bot operators and developers (configured via `BOT_ADMIN_IDS`) | Debug tools, credit management, monitoring |

---

## 3. Architecture

### 3.1 Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| **Runtime** | Bun | Native TypeScript, fast startup, built-in test runner |
| **Bot Framework** | grammY | Mature Telegram framework, TypeScript-first, rich plugin ecosystem (menu, i18n, auto-retry, conversations) |
| **Database** | PostgreSQL + Drizzle ORM | Type-safe schema-as-code, `drizzle-kit push` for auto-migrations, zero manual SQL |
| **LLM Provider** | Google GenAI SDK (`@google/genai`) | Primary and only provider at launch. Gemini for chat/image, Veo for video, Gemini TTS for voice |
| **Provider Contracts** | TypeScript interfaces | `LLMProvider`, `ChatParams`, `ChatResult` — swap providers later without touching handlers |
| **Observability** | OpenTelemetry + `@opentelemetry/sdk-node` | Vendor-neutral tracing/metrics, exportable to Grafana/Jaeger/Logfire |
| **i18n** | `@grammyjs/i18n` | grammY-native, per-user locale resolution, Fluent message format |
| **Validation** | Zod | Runtime config validation, tool parameter schemas, shared with Drizzle |
| **Media Processing** | ffmpeg (system dependency) | Audio/video format conversion (WAV→OGG Opus for TTS, sticker conversion, etc.) |
| **Web Search** | Brave Search API (primary), DuckDuckGo (fallback) | Brave: free tier 1 req/sec with API key. DuckDuckGo: no key needed but unreliable at scale (IP bans) |
| **Linter/Formatter** | Biome | Fast, single-tool replacement for ESLint + Prettier |
| **Scheduler** | In-process with pg-backed persistence | Reminders stored in PostgreSQL, checked on interval |

### 3.2 grammY Plugin Stack

Leverage the grammY ecosystem — don't rebuild what exists:

| Plugin | Package | Purpose |
|--------|---------|---------|
| **parse-mode** | `@grammyjs/parse-mode` | `fmt` template literals for safe HTML formatting, `hydrateReply` for `ctx.replyFmt()` |
| **auto-retry** | `@grammyjs/auto-retry` | Automatic retry on 429/5xx with backoff (API transformer) |
| **throttler** | `@grammyjs/transformer-throttler` | Outgoing request flood control (respects Telegram rate limits) |
| **ratelimiter** | `@grammyjs/ratelimiter` | Incoming per-user rate limiting (3 msgs/2s default) |
| **menu** | `@grammyjs/menu` | Interactive inline keyboards for `/settings` |
| **conversations** | `@grammyjs/conversations` | Multi-step interactions (memory edit, custom prompt, credit transfer) |
| **runner** | `@grammyjs/runner` | Concurrent long-polling with error recovery |
| **i18n** | `@grammyjs/i18n` | Per-user locale with Fluent `.ftl` files |
| **auto-chat-action** | `@grammyjs/auto-chat-action` | Automatic "typing..." indicators while handlers run |

### 3.2 Runtime Flow

```
Telegram Update (long-polling via grammY runner)
    │
    ▼
┌─────────────────────────────────────────────────┐
│              Middleware Stack                     │
│                                                  │
│  1. Error Boundary      (catch-all, user-facing) │
│  2. Logger               (structured, all updates│
│  3. Database Hydrator    (upsert user/chat/msg)  │
│  4. Session              (credit service, tier)  │
│  5. Auto Chat Action     (typing indicators)     │
│  6. Rate Limiter         (per-user throttle)     │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│              Composers (routers)                 │
│                                                  │
│  admin.*           → Bot admin commands          │
│  /start, /help     → Onboarding & help           │
│  /settings         → Interactive menu (Menu)     │
│  /buy, /credits    → Monetization                │
│  payments          → Stars checkout flow         │
│  /imagine, /edit   → Image (command shortcut)    │
│  /video            → Video (command shortcut)    │
│  /tts              → TTS (command shortcut)      │
│  /think            → Deep reasoning (shortcut)   │
│  /remind           → Reminders (command shortcut)│
│  /info             → Message introspection       │
│  @DerpRobot inline → Inline mode                 │
│  mention/reply/DM  → Main chat agent             │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│              LLM Layer                           │
│                                                  │
│  ┌─────────────────────────────────┐             │
│  │ Tool Registry                   │             │
│  │ (tools callable by agent AND    │             │
│  │  as /commands, auto-/help)      │             │
│  └─────────────────────────────────┘             │
│  ┌─────────────────────────────────┐             │
│  │ Google Provider                 │             │
│  │ - Gemini (chat, think, image)   │             │
│  │ - Veo (video)                   │             │
│  │ - Gemini TTS (voice)            │             │
│  └─────────────────────────────────┘             │
│  ┌─────────────────────────────────┐             │
│  │ Context Builder                 │             │
│  │ - Participant registry          │             │
│  │ - Compact message stream        │             │
│  │ - Cache-optimized layout        │             │
│  └─────────────────────────────────┘             │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│         Data Layer (Drizzle + PostgreSQL)         │
│                                                  │
│  users · chats · chat_members · messages         │
│  ledger · usage_quotas · reminders               │
└─────────────────────────────────────────────────┘
```

### 3.3 Architectural Principles

1. **Tool-Command Duality** — Every tool is defined once and produces: (a) grammY command handlers (including aliases), (b) an LLM function tool definition, (c) a `/help` entry, (d) a pricing entry. Adding a new tool = one file.

2. **Tier-Based Model Selection** — Models are selected by tier (`FREE`, `STANDARD`, `PREMIUM`) and capability (`TEXT`, `IMAGE`, `VIDEO`, `VOICE`). Business logic never references model IDs directly.

3. **Middleware Stack** — All cross-cutting concerns (logging, DB persistence, session/credits, rate limiting, typing indicators) live in grammY middleware. Handlers are pure business logic.

4. **Schema-as-Code** — Drizzle ORM schemas are the single source of truth. `drizzle-kit push` applies schema changes directly. `drizzle-kit generate` creates migration files for production.

5. **Compact Context Window** — Chat history is serialized as a participant registry + compact message stream (not per-message JSON blobs). System prompt + participants block forms a stable prefix for LLM cache hits.

6. **Credit-Aware Tools** — Tools check access before execution, return friendly "upgrade" placeholders when credits are insufficient, and deduct only on success with idempotency keys.

7. **Security Boundary** — Tool functions never receive `chatId` or `userId` as arguments. These are derived from the execution context only. This prevents prompt injection attacks where a user tricks the agent into operating on another chat's data.

8. **Modular & Reusable** — Build shared infrastructure once, reuse everywhere. The `reply` module handles message splitting, formatting, and balance footers for every handler. The `credit-gate` wrapper is a single function applied to any tool. The `ToolRegistry` auto-wires commands, help, and LLM schemas from one definition. Every pattern is designed to scale to 50+ tools without architectural changes.

---

## 4. Tool System

### 4.1 Tool Definition

Every capability is defined as a `Tool`. A single definition drives the command handlers, LLM function schema, help text, and pricing.

```typescript
interface ToolDefinition {
  // Identity
  name: string                  // Internal: 'imagine'
  commands: string[]            // Telegram commands: ['/imagine', '/i'] — first is primary, rest are aliases
  description: string           // For LLM function calling
  helpText: string              // For /help display (i18n key)
  category: ToolCategory        // 'media' | 'search' | 'utility' | 'reasoning'

  // Parameters (Zod schema → LLM function params + command arg parsing)
  // NOTE: never include chatId/userId — these are derived from context
  parameters: ZodSchema

  // Execution (same function for command AND agent call)
  execute: (params: T, ctx: ToolContext) => Promise<ToolResult>

  // Pricing
  credits: number               // Flat cost per use (0 = free)
  freeDaily: number             // Daily free uses (0 = paid-only)

  // Model requirement
  capability: ModelCapability    // What model capability it needs
  defaultModel?: string          // Specific model override

  // Access
  minTier?: ModelTier            // Minimum subscription tier
  chatAdminOnly?: boolean        // Restrict to Telegram chat admins (group admins, not bot admins)
}
```

### 4.2 Tool Registry

On startup, the `ToolRegistry`:

1. Registers each tool definition
2. Generates grammY command handlers for all `commands` entries (primary + aliases)
3. Generates LLM function tool schemas (for Gemini function calling)
4. Builds the `/help` message from tool metadata (grouped by `category`)
5. Calls `bot.api.setMyCommands()` per locale to register primary commands with Telegram

### 4.3 Tools Inventory

| Tool | Commands | Credits | Free/day | Capability | Description |
|------|----------|---------|----------|------------|-------------|
| `webSearch` | `/search`, `/s` | 0 | 15 | TEXT | Search the web via Brave Search (DuckDuckGo fallback) |
| `imagine` | `/imagine`, `/i` | 10 | 1 | IMAGE | Generate an image from a prompt |
| `editImage` | `/edit`, `/e` | 10 | 1 | IMAGE | Edit an image (reply to image) |
| `think` | `/think`, `/t` | 5 | 0 | TEXT | Deep reasoning (Gemini Pro) |
| `tts` | `/tts` | 5 | 0 | VOICE | Text-to-speech voice message |
| `video` | `/video`, `/v` | 250 | 0 | VIDEO | Generate a short video (5s) |
| `remind` | `/remind`, `/r` | 0 | 5 | — | Set a one-time or recurring reminder |
| `memory` | — | 0 | ∞ | — | Update persistent chat memory (agent-only) |
| `getMember` | — | 0 | ∞ | — | Get a chat member's profile photo (agent-only) |

Tools with `freeDaily > 0` show the remaining count after use: *"Image generated! (free today: 0 remaining)"*

Tools with `freeDaily = 0` show a contextual upsell when a free user triggers them: *"Video generation requires credits. /buy to get started — plans start at 150⭐/month."*

### 4.4 Free Tier Tool Visibility

**Critical UX decision:** Free-tier users see ALL tools in the agent's function list. When they trigger a paid tool with no free uses left, the tool returns a structured placeholder:

```
[TOOL_UNAVAILABLE] Video generation requires credits.
You have 0 credits. Subscribe from 150⭐/month for the best value.
→ Suggest the user try /buy or explain the feature.
```

The agent then naturally weaves this into conversation: *"I'd love to generate that video for you! You'll need credits for video generation — use /buy to check out the plans."*

---

## 5. Features

### 5.1 Conversational AI (Core)

**Triggers:**
- Direct message in private chat
- Mention: `@DerpRobot`, or name match (`derp`, `дерп`, case-insensitive)
- Reply to a bot message
- `/derp <text>` command

**Behavior:**
1. Extract media from current message (photos, videos, audio, documents, stickers)
2. If the message replies to an older message, fetch that message + its media too (Telegram includes the reply in the update, but the bot also checks the DB for older context)
3. Determine user tier (FREE / STANDARD) based on subscription + credit balance
4. Build optimized context window (see Part 2, §8)
5. Call LLM with context + media + full tool list
6. Send response (text, images, or multi-message)
7. Store the bot's outgoing message in DB with metadata (tools used, credits spent, token usage, model, duration)

**System Prompt & Personality Presets:**

The system prompt is configurable per-chat via `/settings`. Four built-in presets are available, plus a full custom override for subscribers:

| Preset | Description |
|--------|-------------|
| **Default** | Helpful, conversational, naturally opinionated, concise. Adapts tone to match user energy. |
| **Professional** | Formal, structured, thorough. Uses lists and clear formatting. Avoids humor. |
| **Casual** | Very informal, uses slang, emoji-heavy, playful. Leans into humor and wit. |
| **Creative** | Imaginative, poetic, bold. Favors unexpected angles and literary flair. |
| **Custom** | Full system prompt override (subscribers only). Replaces the personality section entirely. |

All presets share the same core rules: respond in user's language, use Telegram MarkdownV2, respect chat memory, stay under 200 words by default.

**Media Input:**

| Type | Sources | Processing |
|------|---------|-----------|
| Photos | Photo messages, image documents, static stickers | Download via `bot.api.getFile()`, pass as binary to Gemini |
| Videos | Video messages, GIFs/animations, video notes, video stickers | Download, convert via ffmpeg if needed, pass as binary |
| Audio | Voice messages, audio files | Download, pass as binary (Gemini handles natively) |
| Documents | PDFs | Download, pass as binary (Gemini handles PDF natively) |
| Stickers | Static (as PNG), animated (as WebM→MP4 via ffmpeg), video (as MP4) | Download + convert to Gemini-compatible format |

ffmpeg is a system dependency (included in Docker image) for format conversions: TTS WAV→OGG Opus, sticker WebM→MP4, audio format normalization.

### 5.2 Image Generation

**Commands:** `/imagine <prompt>`, `/i <prompt>`, or agent tool call

**Flow:**
1. Check credits (1 free/day, then 10 credits)
2. Call Gemini image model with prompt
3. Send generated image as a photo message with the interpreted prompt as caption
4. Deduct credits on success

### 5.3 Image Editing

**Commands:** `/edit <prompt>`, `/e <prompt>` (reply to image), or agent tool call

**Flow:**
1. Extract source image from replied message (or last image in thread if no reply)
2. Check credits (1 free/day, then 10 credits)
3. Call Gemini image model with source image + edit prompt
4. Send edited image
5. Deduct credits on success

### 5.4 Video Generation

**Commands:** `/video <prompt>`, `/v <prompt>`, or agent tool call

**Flow:**
1. Check credits (250 credits, no free tier)
2. Send "Generating video..." progress message
3. Call Veo API (async — poll for completion)
4. Edit progress message periodically ("Still generating...")
5. Download result, send as video message
6. Delete progress message
7. Deduct credits on success

### 5.5 Text-to-Speech

**Command:** `/tts <text>` or agent tool call

**Flow:**
1. Check credits (5 credits, no free tier)
2. Call Gemini TTS model
3. Convert WAV output to OGG Opus via ffmpeg (Telegram voice message format)
4. Send as voice message (uses `sendVoice`, plays inline in Telegram)
5. Deduct credits on success

### 5.6 Deep Thinking

**Commands:** `/think <problem>`, `/t <problem>`, or agent tool call

**Flow:**
1. Check credits (5 credits, no free tier)
2. Call Gemini Pro with extended thinking enabled
3. Return detailed reasoning + answer
4. Deduct credits on success

### 5.7 Web Search

**Trigger:** Agent tool call (autonomous) or `/search <query>`, `/s <query>`

**Flow:**
1. Check daily limit (15 free/day, always free)
2. Call Brave Search API (requires `BRAVE_SEARCH_API_KEY`). If no key configured, fall back to DuckDuckGo (rate-limited to ~1 req/sec, less reliable)
3. Return top results to agent for synthesis
4. Agent weaves results into natural response

### 5.8 Reminders & Scheduled Posts

**Commands:** `/remind <text>`, `/r <text>`, or agent tool call

**Two modes:**
- **Plain reminder:** `createReminder(message: "Call the dentist", fireAt: "2026-02-26T10:00Z")` — stores a text message, sends it verbatim when due. Can include `replyToMessageId` for context.
- **LLM reminder:** `createReminder(prompt: "Search UK news and post a summary", cron: "0 9 * * *", usesLlm: true)` — runs an agent with tools when fired.

**User Commands:**
- `/remind <text>` — Create a reminder (agent parses time/recurrence from natural language)
- `/reminders` — List active reminders with inline "Cancel" buttons

**Agent Tools:**
```
createReminder(description, message?, prompt?, fireAt?, cronExpression?, replyToMessageId?, usesLlm?)
listReminders()
cancelReminder(reminderId)
```

Note: `chatId` is never a tool parameter — it's derived from the current execution context.

**Scheduler Design:**
- Reminders stored in `reminders` table with `fire_at` (one-time) or `cron_expression` (recurring)
- On startup: load all due reminders, fire immediately (with "delayed" note if overdue)
- Every 60 seconds: check for newly due reminders
- When a reminder fires:
  - **Plain:** Send the stored `message` text to the chat (optionally as reply to `reply_to_message_id`)
  - **LLM:** Create an agent call with the stored `prompt` + tools, post result to chat
- After firing: one-time reminders marked `completed`, recurring reminders compute next `fire_at`

**Abuse Controls:**
- Max 10 active reminders per user per chat
- Max 3 recurring reminders per user total
- Recurring interval minimum: 1 hour
- Reminders auto-expire after 365 days if not fired
- Only reminder creator or chat admin can cancel
- **Configurable per chat:** admins can toggle whether non-admins can create reminders (via `/settings`)

### 5.9 Chat Memory

**Commands:**
- `/memory` — View current chat memory
- `/memory_set <text>` — Set memory (access controlled per chat settings)
- `/memory_clear` — Clear memory (access controlled per chat settings)

Commands use underscores so Telegram treats them as clickable single commands (clicking `/memory set` in chat would only send `/memory`).

**Agent tool:** `updateMemory(text)` — agent autonomously updates memory (respects same access control)

**Design:**
- Stored per-chat in `chats.memory` (TEXT, max 4096 chars — increased from 1024)
- Appended to system prompt on every request
- Positioned at a fixed location in the prompt to maximize LLM cache stability
- **Access control is configurable per chat** via `/settings`:
  - `admins` — Only chat admins can edit memory (default for groups/supergroups)
  - `everyone` — Any member can edit memory (default for private chats)
- The agent is instructed to update memory when it learns persistent facts (user preferences, nicknames, ongoing topics)

### 5.10 Inline Mode

**Trigger:** `@DerpRobot <query>` in any chat

**Behavior (deferred generation):**

To avoid generating a response on every keystroke ("h", "he", "hel", "hell", "hello"...), inline mode uses a **placeholder → edit** pattern:

1. On inline query: immediately return an `InlineQueryResultArticle` with a placeholder (e.g., "Thinking...")
2. Cache the query per-user (`is_personal: true`, `cache_time: 5`)
3. When the user selects the result: Telegram sends the placeholder message
4. Bot receives the sent message via `chosen_inline_result` or detects the placeholder
5. Bot generates the actual LLM response and edits the message in-place

**Model selection:**
- **Free users:** FREE tier (Flash Lite), text-only
- **Subscribers:** STANDARD tier (Flash), text-only

No tools in inline mode due to latency constraints.

### 5.11 Settings (Interactive Menu)

**Command:** `/settings`

Uses grammY `Menu` plugin for an interactive inline keyboard:

```
⚙️ Chat Settings
├── 🎭 Personality: Default
│   ├── Default
│   ├── Professional
│   ├── Casual
│   ├── Creative
│   └── ✏️ Custom (subscribers only)
├── 🌐 Language: English
│   ├── 🇬🇧 English
│   ├── 🇷🇺 Русский
│   └── 🔄 Auto-detect
├── 🧠 Memory (1,247 / 4,096 chars)
│   ├── 📖 View
│   ├── ✏️ Edit
│   └── 🗑 Clear
├── 🔒 Permissions
│   ├── Memory editing: Admins only ⇄ Everyone
│   └── Reminders: Admins only ⇄ Everyone
├── 💰 Balance: 245 credits
│   ├── 📊 This month's usage
│   └── 🛒 Buy credits
└── ✕ Close
```

The "Edit" options for memory and custom prompt use grammY's `conversations` plugin — the bot asks "Send your new text:" and waits for the next message.

Permission toggles are simple inline button toggles (no conversation needed).

### 5.12 Group Onboarding

**`my_chat_member` update:** When the bot is added to a group, it sends an interactive onboarding:

```
👋 Hey everyone! I'm Derp, your AI assistant.

Mention me (@DerpRobot) or just say "derp" to chat.
Type /help to see what I can do.

Quick setup for admins:

Who can edit chat memory?
[Admins only]  [Everyone]

Who can create reminders?
[Admins only]  [Everyone]
```

This sets initial permissions without requiring `/settings`. Defaults: admins-only for groups, everyone for private chats.

### 5.13 Onboarding — Private Chat (`/start`)

**First-time users** get a guided interactive experience + a **25 credit welcome bonus** (enough for 2 images + 1 TTS) so they immediately experience STANDARD tier:

```
👋 Hey! I'm Derp — your AI assistant in Telegram.

🎁 You got 25 free credits to try premium features!

Here's what I can do:

💬 Chat — Just mention me or reply to me
🎨 Images — /imagine a sunset over Tokyo
🔍 Search — I search the web automatically
🎬 Video — /video a cat riding a skateboard
🗣 Voice — /tts Hello world
🧠 Think — /think solve this math puzzle
⏰ Remind — /remind call mom tomorrow at 6pm

[Try it! Ask me anything →]  [See all commands →]
```

The welcome bonus is recorded as a `grant` ledger entry with idempotency key `welcome:{telegram_id}` (never granted twice).

### 5.14 Help (Auto-Generated)

**Command:** `/help`

Auto-generated from the Tool Registry, grouped by category:

```
🤖 Derp Commands

💬 Chat
  Just mention me (@DerpRobot) or say "derp" — I'll respond!

🔍 Search & Research
  /search <query> — Search the web (15 free/day)
  /think <problem> — Deep reasoning (5 credits)

🎨 Creative
  /imagine <prompt> — Generate an image (1 free/day, then 10 credits)
  /edit <prompt> — Edit an image (reply to image, 1 free/day, then 10 credits)
  /video <prompt> — Generate a video (250 credits)
  /tts <text> — Text to speech (5 credits)

⏰ Reminders
  /remind <text> — Set a reminder (5 free/day)
  /reminders — View active reminders

⚙️ Settings & Account
  /settings — Chat settings, memory & permissions
  /credits — Check balance
  /buy — Purchase credits
  /info — Reply to my message to see details

💡 Tip: I can do all of this automatically — just describe what you need!
```

### 5.15 Message Introspection (`/info`)

**Command:** `/info` (reply to a Derp message)

Shows metadata about how the bot generated a specific response:

```
📊 Message Info

Model: gemini-2.5-flash
Tier: STANDARD
Tokens: 1,234 in / 567 out (cache hit: 890)
Tools used: webSearch, imagine
Credits spent: 10
Duration: 2.3s
```

This is powered by the `metadata` JSONB column stored on every outgoing bot message (see §9.5).

### 5.16 Bot Admin Commands

| Command | Description |
|---------|-------------|
| `/admin status` | System diagnostics (uptime, DB stats, active reminders, cache hit rate) |
| `/admin credits <n> [user\|chat]` | Grant credits |
| `/admin reset [user\|chat]` | Reset credits to 0 |
| `/admin tools` | List all tools with pricing and margin analysis |
| `/admin buy` | Test purchase flow (1⭐) |
| `/admin refund <charge_id>` | Process refund |

Bot admin commands restricted to `BOT_ADMIN_IDS` env var. These are bot owners, distinct from Telegram chat admins.

---

## 6. Telegram API Integration

### 6.1 BotFather Setup Requirements

Before the bot works correctly in groups:
- **Disable Group Privacy mode** in BotFather (`/mybots` → Bot Settings → Group Privacy → Turn off). Without this, the bot only receives messages that @mention it or are commands — the context window would be empty.
- **Enable Inline Mode** in BotFather for `@DerpRobot` inline queries.
- **Enable Payments** via `@BotFather` → Payments to accept Telegram Stars.

### 6.2 Allowed Updates

```typescript
const ALLOWED_UPDATES = [
  'message',            // Regular messages + successful_payment
  'edited_message',     // Edits → update stored text in DB (invalidates LLM cache, acceptable)
  'callback_query',     // Inline keyboard button clicks (menus, settings, buy)
  'inline_query',       // Inline mode (@DerpRobot query)
  'chosen_inline_result', // When user selects an inline result (for deferred generation)
  'pre_checkout_query', // Payment validation
  'my_chat_member',     // Bot added/removed from chats
  'chat_member',        // Member changes (for participant tracking)
  'message_reaction',   // Reactions on bot messages (quality feedback)
]
```

### 6.3 Command Registration

On startup, call `setMyCommands` per locale (auto-generated from Tool Registry):

```typescript
await bot.api.setMyCommands([
  { command: 'help',      description: 'Show available commands' },
  { command: 'imagine',   description: 'Generate an image' },
  { command: 'edit',      description: 'Edit an image (reply to image)' },
  { command: 'video',     description: 'Generate a video' },
  { command: 'tts',       description: 'Text to speech' },
  { command: 'think',     description: 'Deep reasoning' },
  { command: 'search',    description: 'Search the web' },
  { command: 'remind',    description: 'Set a reminder' },
  { command: 'reminders', description: 'View active reminders' },
  { command: 'settings',  description: 'Chat settings' },
  { command: 'credits',   description: 'Check your balance' },
  { command: 'buy',       description: 'Purchase credits' },
  { command: 'info',      description: 'Message details (reply to my message)' },
], { language_code: 'en' })

// Russian, etc.
await bot.api.setMyCommands([...], { language_code: 'ru' })
```

### 6.4 Forum Topics Support

For supergroups with topics enabled (`is_forum: true`):
- Context window only includes messages from the **same topic** (`thread_id`)
- Participants list is scoped to those who posted in that topic
- Reminders fire in the topic they were created in
- The `thread_id` is stored on messages and reminders

### 6.5 Message Delivery (common/reply.ts)

All bot responses go through a shared `reply` module that handles:

1. **Message splitting** — Telegram caps messages at 4096 characters. The module splits on paragraph → sentence → word boundaries, preserving formatting. Each chunk is sent sequentially with `reply_to_message_id` only on the first.
2. **Format-safe rendering** — Uses `@grammyjs/parse-mode` `fmt` template literals for safe HTML formatting. No manual escaping. Falls back to plain text on parse errors.
3. **Balance footer** — Appends credit usage line (§7.10) to the last message chunk when credits were deducted.
4. **Media + text** — Sends images as photo messages with caption. If caption exceeds 1024 chars (Telegram caption limit), sends photo first, then text as a follow-up.
5. **Error wrapping** — Catches `sendMessage` failures, retries once with plain text fallback.

This module is used by every handler and every tool — a single implementation for all response delivery.

### 6.6 Bot API Methods Used

| API Method | Purpose |
|-----------|---------|
| `getFile` + download | Media extraction for LLM input |
| `getChat` | Rich chat info (bio, description, linked chat) — cached in DB |
| `getChatMember` | Member info (bio, custom title) + admin check — cached in DB |
| `getUserProfilePhotos` | Agent tool: get user's profile photo for editing |
| `getChatAdministrators` | Bulk admin list on first chat interaction — cached |
| `sendInvoice` / `createInvoiceLink` | Telegram Stars payments + subscriptions |
| `answerPreCheckoutQuery` | Payment validation |
| `refundStarPayment` | Process refunds |
| `setMyCommands` | Register commands per locale on startup |
| `sendChatAction` | Typing indicators |
| `setMessageReaction` | React to messages (e.g., 👌 for empty responses) |
| `editMessageText` | Update progress messages (video gen) + inline mode deferred generation |
| `deleteMessage` | Clean up progress messages |
