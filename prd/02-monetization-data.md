# Derp PRD v2 — Part 2: Monetization & Data Model

---

## 7. Monetization

### 7.1 Philosophy

The monetization model optimizes for **recurring revenue** (subscriptions) while offering **top-up flexibility** (credit packs). Subscriptions grant credits at a steep discount, making them the obviously better deal — the top-up packs serve as **decoy pricing** that drives subscription conversion.

**Core rules:**
- **Credits** are the universal currency. Every paid tool has a flat credit cost.
- **Subscriptions** grant a monthly credit allowance + model tier upgrade.
- **Top-up packs** are available for non-subscribers (or subscribers who need more).
- **Group pools** let any member fund a shared credit balance for a group chat.
- **No variable pricing.** Users always know what a tool costs before using it.

### 7.2 Pricing Foundation

Based on actual market data (February 2026):

| Parameter | Value |
|-----------|-------|
| Developer payout per Star | **$0.013** (after Telegram + store fees) |
| User purchase cost per Star | **~$0.020** (varies by platform) |
| Stars needed to net $1.00 | **~77 Stars** |
| Subscription period | **30 days only** (Telegram limitation) |
| Min invoice | **1 Star** |

### 7.3 Model Tiers

| Tier | Who gets it | Chat Model | Context | Tools |
|------|------------|-----------|---------|-------|
| **FREE** | Everyone (no credits, no subscription) | Gemini 2.5 Flash Lite | 15 messages | Daily free limits on select tools |
| **STANDARD** | Any user with credits > 0 OR active subscription | Gemini 2.5 Flash | 100 messages | All tools (credit-gated) |

The tier upgrade is automatic: the moment you have credits (from any source), you get STANDARD. When credits hit 0 and no subscription is active, you fall back to FREE.

Premium model access (Gemini Pro for `/think`, Veo for `/video`) is handled per-tool, not per-tier.

### 7.4 Subscriptions (Telegram Stars, Auto-Renewing)

Telegram natively supports Star subscriptions with auto-renewal via `createInvoiceLink` with `subscription_period: 2592000` (30 days).

| Plan | Stars/month | Credits/month | ⭐ per credit | Savings vs top-up | Revenue/mo |
|------|------------|--------------|--------------|-------------------|------------|
| **Lite** | 150⭐ | 200 | 0.75 | 25% | $1.95 |
| **Pro** | 500⭐ | 750 | 0.67 | 33% | $6.50 |
| **Ultra** | 1500⭐ | 2500 | 0.60 | 40% | $19.50 |

**Decoy dynamics:**
- Lite is the anchor. 150⭐ for 200 credits. Functional but limited.
- Pro is the **target plan**. For 3.3x the price of Lite, you get 3.75x the credits. It's the obvious sweet spot. Labeled `POPULAR` in the UI.
- Ultra is for power users (video generation, heavy usage). Labeled `BEST VALUE`. The large allocation is the draw — a Pro user generating 3 videos/month would be better off on Ultra.

**Subscription benefits beyond credits:**
- STANDARD model (Gemini Flash) in all chats
- 100-message context window (vs 15 for free)
- Access to all tools (subject to credit cost)
- STANDARD model in inline mode
- Custom system prompt override in `/settings`

### 7.5 Top-Up Credit Packs (One-Time Purchase)

Available to everyone. Priced to make subscriptions look better.

| Pack | Stars | Credits | ⭐ per credit | Revenue |
|------|-------|---------|--------------|---------|
| **Micro** | 50⭐ | 50 | 1.00 | $0.65 |
| **Small** | 150⭐ | 150 | 1.00 | $1.95 |
| **Medium** | 500⭐ | 550 | 0.91 | $6.50 |
| **Large** | 1500⭐ | 1800 | 0.83 | $19.50 |

**Decoy analysis:**
- **Small (150⭐ → 150 credits)** is the primary decoy. Same price as Lite subscription (150⭐), but Lite gives 200 credits/month + model upgrade. Any rational buyer sees the subscription is better.
- **Medium (500⭐ → 550 credits)** is the secondary decoy for Pro. Same price, but Pro gives 750 credits/month + model upgrade. The subscription is 36% more value, *recurring*.

### 7.6 `/buy` UX Flow

```
💰 Get Credits

⭐ Subscriptions (monthly, best value!)
  ├── [Lite — 150⭐/mo → 200 credits (25% savings)]
  ├── [Pro — 500⭐/mo → 750 credits (33% savings)] ← POPULAR
  └── [Ultra — 1500⭐/mo → 2,500 credits (40% savings)] ← BEST VALUE

📦 Credit Packs (one-time)
  ├── [Micro — 50⭐ → 50 credits]
  ├── [Small — 150⭐ → 150 credits]
  ├── [Medium — 500⭐ → 550 credits (+10%)]
  └── [Large — 1500⭐ → 1,800 credits (+20%)]

💡 Subscriptions renew monthly and include a model upgrade.
```

Subscriptions are listed first (above the fold) to maximize visibility.

**In group chats**, `/buy` shows an additional section:
```
🏠 Fund this group's credit pool
  ├── [Add 100 credits — 100⭐]
  ├── [Add 500 credits — 450⭐ (10% bonus)]
  └── [Transfer from my balance → (min 100 credits)]
```

Group pool credits give STANDARD tier to ALL members in that group.

**Credit transfer:** subscribers (or anyone with credits) can transfer from their personal balance to the group pool. Minimum 100 credits per transfer to prevent gaming (adding 1 credit to unlock STANDARD for the whole group).

### 7.7 Tool Pricing (Flat-Rate)

Pricing based on actual API costs with healthy margins:

| Tool | Credits | Free daily | API cost | Revenue at Pro rate | Margin |
|------|---------|-----------|----------|--------------------|---------|
| Chat (FREE tier) | 0 | ∞ | $0.00017 | — | — |
| Chat (STANDARD tier) | 0 | ∞ | $0.00089 | — | — |
| `webSearch` | 0 | 15 | ~$0 | — | — |
| `imagine` | 10 | 1 | $0.039 | $0.087 | 123% |
| `editImage` | 10 | 1 | $0.039 | $0.087 | 123% |
| `tts` | 5 | 0 | $0.020 | $0.043 | 115% |
| `think` | 5 | 0 | $0.004 | $0.043 | 975% |
| `video` | 250 | 0 | $0.750 | $2.17 | 189% |
| `remind` | 0 | 5 | $0 | — | — |
| `memory` | 0 | ∞ | $0 | — | — |

**API cost sources (Feb 2026):**
- Image: Gemini native image output, ~1,290 tokens at $30/1M = $0.039
- Video: Veo 3.1 Fast, $0.15/second × 5s default = $0.75
- TTS: Gemini 2.5 Pro TTS, ~32 tokens/second audio output at $20/1M, 100 words ≈ $0.02
- Think: Gemini 3 Pro, ~650 input + 250 output tokens at $2/$12 per 1M = $0.004
- Chat (Flash): ~650 input + 250 output tokens at $0.40/$2.50 per 1M = $0.00089
- Chat (Flash Lite): ~650 input + 250 output tokens at $0.10/$0.40 per 1M = $0.00017

**Video pricing rationale:** At the Pro rate (0.67⭐/credit), 250 credits ≈ 167⭐ ($2.17 revenue). API cost is $0.75. Margin is 189%. A Pro subscriber with 750 credits can generate 3 videos/month — heavy video users are nudged toward Ultra (2500 credits = 10 videos) or top-up packs at full rate.

### 7.8 Credit Sources & Deduction Priority

**Credit pools (two types):**
1. **User credits** — Personal balance, usable in any chat
2. **Chat credits** — Group pool, usable only in that group, benefits all members

**Deduction priority:**
1. Free daily limit (if available for this tool)
2. Chat credits (group pool first — funded by the group, used by the group)
3. User credits (personal balance as fallback)

**Subscription credits** are deposited into the user's personal balance on renewal. Users can transfer personal credits to a group pool via `/buy` (min 100 credits).

### 7.9 Payments Flow (Telegram Stars)

**One-time pack purchase:**
1. User taps a pack button from `/buy`
2. Bot calls `sendInvoice` with `currency: "XTR"`, no `provider_token`
3. User confirms → Telegram sends `pre_checkout_query`
4. Bot validates and calls `answerPreCheckoutQuery(ok: true)`
5. Telegram processes payment → sends `successful_payment` in message
6. Bot adds credits via `CreditService.purchase()`
7. Idempotency: `telegram_charge_id` prevents double-crediting

**Subscription:**
1. User taps a subscription button from `/buy`
2. Bot calls `createInvoiceLink` with `subscription_period: 2592000` (30 days)
3. User opens link → subscribes
4. On each renewal: Telegram sends `successful_payment` with `is_recurring: true`
5. Bot credits the monthly allowance
6. User can cancel from Telegram's subscription management

**Credit transfer (personal → group):**
1. User taps "Transfer from my balance" in group `/buy`
2. Bot asks amount (min 100) via conversation
3. Bot deducts from user balance, adds to chat balance
4. Records `transfer` transaction in ledger for both sides

**Refunds:**
- `refundStarPayment(userId, telegramPaymentChargeId)`
- Reverses the original transaction in our ledger
- Idempotency key: `refund:{charge_id}`

### 7.10 Credit Balance Display

After every paid tool use, the bot appends a subtle balance indicator:

```
[Generated image]
✨ 10 credits used · 245 remaining
```

When balance drops below **20 credits** (checking both personal and active group pool):

```
[Generated image]
⚠️ 10 credits used · 12 remaining · /buy to top up
```

At zero balance:

```
Your credits have run out. You're now on the free tier (Flash Lite model).
/buy to get more credits, or keep using free features!
```

### 7.11 Bot Message Metadata Tracking

Every outgoing bot message is stored in the `messages` table with a `metadata` JSONB column containing:

```json
{
  "model": "gemini-2.5-flash",
  "tier": "STANDARD",
  "inputTokens": 1234,
  "outputTokens": 567,
  "cacheHitTokens": 890,
  "toolsUsed": ["webSearch", "imagine"],
  "creditsSpent": 10,
  "creditSource": "user",
  "durationMs": 2341
}
```

This enables:
- `/info` command (reply to bot message → see generation details)
- Transparency for users about resource usage
- Cost analysis and margin tracking for bot admins
- Quality correlation (link reaction feedback to specific models/prompts)

---

## 8. Context Window Design

### 8.1 Problem

The current Python implementation sends each message as a full JSON object with repeated sender info. For 100 messages, this wastes thousands of tokens on duplicate `user_id`, `name`, `username` fields.

### 8.2 Optimized Format

The context is split into three sections with distinct stability characteristics:

**Section 1: System Prompt (STABLE — cached by Gemini)**
```
You are Derp, a helpful and conversational assistant...
[personality preset + guidelines]

## Chat Memory
[persistent memory from database — changes rarely]
```

**Section 2: Chat & Participants (SEMI-STABLE)**
```
# CHAT
Type: supergroup | Title: Солнышки | ID: -1001174590460
Description: A chat about cats and sunflowers
Bio: Est. 2019

# PARTICIPANTS (in current context)
- John (id:12345, @johndoe) [admin] — Bio: Cat lover
- Alice (id:67890, @alice_w) — Bio: Photographer
- Bob (id:11111)
- Derp (id:99999, @DerpRobot) [bot, self]
```

**Participant data enrichment:**
- `getChat` is called on first interaction with a chat. Results (description, bio, linked_chat) are **cached in the `chats` table** and refreshed periodically (e.g., every 24h).
- `getChatMember` is called to get bio and custom title. Results are **cached in `chat_members` table**.
- Telegram has no "get all members" API. We track members **optimistically**: every incoming message upserts the sender. `chat_member` updates add/remove members. On first interaction, `getChatAdministrators` bulk-loads admins.
- **Only participants who appear in the current message history** are included in the PARTICIPANTS block. This keeps the list manageable for large groups (500+ members). An agent tool `listAllMembers` is available if the LLM needs the full roster.

**Section 3: Message Stream (DYNAMIC — grows with each message)**
```
# MESSAGES (last 100)
[4521] John: Hey everyone, what's up?
[4522] Alice: Not much, just chilling
[4523] Bob: [photo file_id:AgACAgIAAxk] Check out this sunset
[4524] John (→4523): Wow that's beautiful!
[4525] Alice: [voice file_id:AwACAgIAAxk]
[4526] Derp: That's a gorgeous sunset, Bob! The colors remind me of...
[4527] John: @DerpRobot what do you think about pineapple on pizza?
```

**Format rules:**
- `[msg_id]` — Telegram message ID for reply references
- `(→msg_id)` — Reply-to indicator
- Media tags include `file_id` so tools can download/reference them: `[photo file_id:AgACAgIAAxk]`, `[voice file_id:AwACAgIAAxk]`, `[video file_id:BAACAgIAAxk]`, `[sticker file_id:CAACAgIAAxk]`, `[document: report.pdf file_id:BQACAgIAAxk]`
- Sender identified by `first_name` only (full info in PARTICIPANTS block)
- No timestamps in messages (reduces tokens; date context available in system prompt)
- `[bot, self]` tag on the bot's own participant entry

### 8.3 Token Savings

| Metric | Old format (JSON) | New format (compact) | Savings |
|--------|-------------------|---------------------|---------|
| 100 messages, 5 participants | ~12,000 tokens | ~3,500 tokens | **71%** |
| 15 messages, 3 participants | ~2,200 tokens | ~700 tokens | **68%** |

### 8.4 Cache Optimization for Gemini

Gemini automatically caches repeated prefixes across requests. To maximize cache hits:

1. **System prompt is always identical** — No dynamic content except chat memory (which changes rarely)
2. **Participants block is stable** — Only changes when the active participant set changes. Sorted by `telegram_id` (deterministic order)
3. **Messages are append-only** — New messages are added to the end. Old messages are never modified or reordered
4. **No timestamps in messages** — Timestamps would make every message unique across requests

**Handling edited messages:** `edited_message` updates are processed: the stored text is updated in the database. When the next context window is built, the edited message has new content. This invalidates the cache from that point forward, which is acceptable — edits are infrequent.

**Cache hit pattern:**
```
Request N:   [system prompt][participants][msg1..msg99]   + [msg100 = new message]
Request N+1: [system prompt][participants][msg1..msg100]  + [msg101 = new message]
                ↑ cached prefix (identical)                  ↑ new suffix
```

### 8.5 Reply Context & Forum Topics

**Reply context:** Telegram includes the replied-to message in the update payload. The bot uses this directly for the current request. For building the context window from DB, if a message references a `reply_to_message_id` that's outside the current window, it's fetched from DB and prepended as a reference:

```
# REFERENCED MESSAGE (outside current window)
[1234] Alice: [photo file_id:AgACAgIAAxk] This was my vacation photo

# MESSAGES (last 100)
...
[5678] John (→1234): Can you explain what's in this photo?
```

**Forum topics:** For supergroups with `is_forum: true`, the context window is scoped to the specific `thread_id`. Only messages from the same topic are included. Participants are also scoped to those who posted in that topic.

---

## 9. Data Model

### 9.1 Entity Relationship

```
┌──────────┐       ┌──────────────┐       ┌──────────────────────┐
│  users   │──1:N──│ chat_members  │──N:1──│        chats         │
│          │       │              │       │                      │
│ id (uuid)│       │ user_id (FK) │       │ id (uuid)            │
│ tg_id    │       │ chat_id (FK) │       │ tg_id                │
│ credits  │       │ role         │       │ credits              │
│ sub_tier │       │ bio (cached) │       │ memory               │
│ sub_exp  │       │ last_seen_at │       │ custom_prompt        │
└──────────┘       └──────────────┘       │ settings (jsonb)     │
     │                                     └──────────────────────┘
     │             ┌──────────────┐              │
     ├──1:N───────│  messages     │──N:1─────────┤
     │             │              │              │
     │             │ text         │              │
     │             │ direction    │              │
     │             │ metadata     │              │
     │             └──────────────┘              │
     │                                           │
     │             ┌──────────────────┐          │
     ├──1:N───────│   ledger          │──N:1────┤
     │             │                  │          │
     │             │ type (enum)      │          │
     │             │ amount           │          │
     │             │ balance_after    │          │
     │             │ idempotency_key  │          │
     │             └──────────────────┘          │
     │                                           │
     │             ┌──────────────────┐          │
     ├──1:N───────│   usage_quotas    │──N:1────┤
     │             │                  │
     │             │ usage_date       │
     │             │ counters (jsonb) │
     │             └──────────────────┘
     │
     │             ┌──────────────────┐
     └──1:N───────│   reminders       │──N:1── chats
                   │                  │
                   │ message / prompt │
                   │ fire_at / cron   │
                   │ status           │
                   └──────────────────┘
```

### 9.2 Table: `users`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | Internal ID |
| `telegram_id` | BIGINT | UNIQUE, NOT NULL, indexed | Telegram user ID |
| `is_bot` | BOOLEAN | NOT NULL, default `false` | |
| `first_name` | VARCHAR(255) | NOT NULL | |
| `last_name` | VARCHAR(255) | nullable | |
| `username` | VARCHAR(255) | nullable | |
| `language_code` | VARCHAR(10) | nullable | ISO 639-1 |
| `is_premium` | BOOLEAN | NOT NULL, default `false` | Telegram Premium flag |
| `credits` | INTEGER | NOT NULL, default `0`, CHECK `>= 0` | Personal credit balance |
| `subscription_tier` | VARCHAR(10) | nullable | `'lite'`, `'pro'`, `'ultra'`, or `null` |
| `subscription_expires_at` | TIMESTAMPTZ | nullable | When current period ends |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | Auto-updated |

### 9.3 Table: `chats`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `telegram_id` | BIGINT | UNIQUE, NOT NULL, indexed | |
| `type` | VARCHAR(20) | NOT NULL | `private`, `group`, `supergroup`, `channel` |
| `title` | VARCHAR(255) | nullable | Group/channel title |
| `username` | VARCHAR(255) | nullable | |
| `first_name` | VARCHAR(255) | nullable | For private chats |
| `last_name` | VARCHAR(255) | nullable | For private chats |
| `is_forum` | BOOLEAN | NOT NULL, default `false` | Topics enabled |
| `description` | TEXT | nullable | Cached from `getChat` |
| `memory` | TEXT | nullable | Persistent agent memory (max 4096 enforced in app) |
| `custom_prompt` | TEXT | nullable | Custom system prompt override (subscribers) |
| `personality` | VARCHAR(20) | NOT NULL, default `'default'` | `default`, `professional`, `casual`, `creative`, `custom` |
| `settings` | JSONB | NOT NULL, default `{}` | Chat-specific settings (see below) |
| `credits` | INTEGER | NOT NULL, default `0`, CHECK `>= 0` | Group credit pool |
| `language_code` | VARCHAR(10) | nullable | Chat-level language override |
| `cached_at` | TIMESTAMPTZ | nullable | When `getChat` data was last refreshed |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**`settings` JSONB structure:**
```json
{
  "memoryAccess": "admins" | "everyone",
  "remindersAccess": "admins" | "everyone"
}
```

### 9.4 Table: `chat_members`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `chat_id` | UUID | FK → `chats`, NOT NULL | |
| `user_id` | UUID | FK → `users`, NOT NULL | |
| `role` | VARCHAR(20) | NOT NULL, default `'member'` | `creator`, `administrator`, `member`, `restricted`, `left`, `kicked` |
| `custom_title` | VARCHAR(255) | nullable | Cached from `getChatMember` |
| `bio` | VARCHAR(255) | nullable | Cached from `getChatMember` |
| `last_seen_at` | TIMESTAMPTZ | NOT NULL, default `now()` | Last message from this user in this chat |
| `is_active` | BOOLEAN | NOT NULL, default `true` | `false` when left/kicked |
| `cached_at` | TIMESTAMPTZ | nullable | When `getChatMember` data was last refreshed |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**Unique constraint:** `(chat_id, user_id)`

**Populated from:**
- Every incoming message → upsert with `last_seen_at = now()` (optimistic tracking)
- `chat_member` updates → update `role`, `is_active`
- `getChatAdministrators` on first chat interaction → bulk upsert with roles
- `getChatMember` called per-user for bio/title → cached with `cached_at`, refreshed every 24h

### 9.5 Table: `messages`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `chat_id` | UUID | FK → `chats`, NOT NULL, indexed | |
| `user_id` | UUID | FK → `users`, nullable | Null for bot's outgoing messages with no user context |
| `telegram_message_id` | INTEGER | NOT NULL | |
| `thread_id` | INTEGER | nullable | Forum topic ID |
| `direction` | VARCHAR(3) | NOT NULL | `'in'` or `'out'` |
| `content_type` | VARCHAR(20) | nullable | `text`, `photo`, `video`, `voice`, `sticker`, `document`, etc. |
| `text` | TEXT | nullable | Message text or caption |
| `reply_to_message_id` | INTEGER | nullable | Telegram msg ID being replied to |
| `attachment_file_id` | VARCHAR(255) | nullable | Telegram file_id for media |
| `metadata` | JSONB | nullable | Bot response metadata (model, tokens, tools, credits, duration) |
| `telegram_date` | TIMESTAMPTZ | NOT NULL | When Telegram recorded the message |
| `edited_at` | TIMESTAMPTZ | nullable | Last edit timestamp (updated on `edited_message`) |
| `deleted_at` | TIMESTAMPTZ | nullable | Soft delete |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**Unique constraint:** `(chat_id, telegram_message_id)`

The `metadata` column is populated only for `direction: 'out'` messages (bot responses). See §7.11 for the schema.

### 9.6 Table: `ledger`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `user_id` | UUID | FK → `users`, NOT NULL, indexed | Who |
| `chat_id` | UUID | FK → `chats`, nullable, indexed | Which group (null = personal) |
| `type` | VARCHAR(20) | NOT NULL | `purchase`, `subscription`, `spend`, `refund`, `grant`, `transfer` |
| `amount` | INTEGER | NOT NULL | Positive = credit, negative = debit |
| `balance_after` | INTEGER | NOT NULL | Snapshot for audit trail |
| `tool_name` | VARCHAR(50) | nullable | Which tool was used (for `spend`) |
| `description` | VARCHAR(255) | nullable | Human-readable note |
| `telegram_charge_id` | VARCHAR(255) | nullable | Telegram payment reference |
| `idempotency_key` | VARCHAR(255) | nullable, UNIQUE | Prevent duplicate transactions |
| `metadata` | JSONB | nullable | Arbitrary context |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**Transaction types:**
- `purchase` — One-time credit pack purchase
- `subscription` — Monthly subscription credit grant
- `spend` — Tool usage deduction
- `refund` — Reversed purchase/subscription
- `grant` — Bot-admin-granted credits
- `transfer` — User → group pool transfer (records two entries: debit on user, credit on chat)

### 9.7 Table: `usage_quotas`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `user_id` | UUID | FK → `users`, NOT NULL | |
| `chat_id` | UUID | FK → `chats`, NOT NULL | |
| `usage_date` | DATE | NOT NULL | |
| `counters` | JSONB | NOT NULL, default `{}` | `{"webSearch": 3, "imagine": 1}` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**Unique constraint:** `(user_id, chat_id, usage_date)`

### 9.8 Table: `reminders`

| Column | Type | Constraints | Notes |
|--------|------|------------|-------|
| `id` | UUID | PK, default `gen_random_uuid()` | |
| `chat_id` | UUID | FK → `chats`, NOT NULL, indexed | Where to post |
| `user_id` | UUID | FK → `users`, NOT NULL, indexed | Who created it |
| `thread_id` | INTEGER | nullable | Forum topic (fire in same topic) |
| `message` | TEXT | nullable | Plain text to send (for non-LLM reminders) |
| `prompt` | TEXT | nullable | LLM prompt to execute (for LLM reminders) |
| `reply_to_message_id` | INTEGER | nullable | Message to reply to when firing |
| `fire_at` | TIMESTAMPTZ | nullable | Next fire time |
| `cron_expression` | VARCHAR(100) | nullable | Cron expression for recurring |
| `is_recurring` | BOOLEAN | NOT NULL, default `false` | |
| `uses_llm` | BOOLEAN | NOT NULL, default `false` | Whether to run LLM with tools when firing |
| `status` | VARCHAR(20) | NOT NULL, default `'active'` | `active`, `completed`, `cancelled`, `failed`, `expired` |
| `last_fired_at` | TIMESTAMPTZ | nullable | |
| `fire_count` | INTEGER | NOT NULL, default `0` | Total times fired |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` | |

**Indexes:**
- `(status, fire_at)` — For scheduler queries: "find active reminders due now"
- `(user_id, chat_id)` — For listing a user's reminders in a chat

---

## 10. LLM Integration

### 10.1 Provider Contract

Ship with Google only. Build against interfaces for future portability.

```typescript
interface LLMProvider {
  chat(params: ChatParams): Promise<ChatResult>
  generateImage?(params: ImageParams): Promise<Buffer>
  generateVideo?(params: VideoParams): Promise<Buffer>
  synthesizeSpeech?(params: TTSParams): Promise<Buffer>
}

interface ChatParams {
  model: string
  systemPrompt: string
  userPrompt: string              // Compact context string (participants + messages)
  media?: BinaryAttachment[]      // Photos, videos, audio from current message
  tools?: ToolSchema[]            // Function calling definitions
  maxOutputTokens?: number
  thinkingEnabled?: boolean       // For /think
}

interface ChatResult {
  text: string
  images?: Buffer[]               // Inline image generation (Gemini native)
  usage: TokenUsage
  toolCalls?: ToolCall[]
}

interface TokenUsage {
  inputTokens: number
  outputTokens: number
  cacheHitTokens?: number         // Track cache effectiveness
}
```

### 10.2 Model Registry

| Model ID | Capability | Tier | Default | Pricing |
|----------|-----------|------|---------|---------|
| `gemini-2.5-flash-lite` | TEXT | FREE | Yes | $0.10/$0.40 per 1M tokens |
| `gemini-2.5-flash` | TEXT | STANDARD | Yes | $0.40/$2.50 per 1M tokens |
| `gemini-3-pro-preview` | TEXT | PREMIUM | Yes | $2.00/$12.00 per 1M tokens |
| `gemini-2.5-flash-preview-image` | IMAGE | STANDARD | Yes | ~$0.039/image |
| `veo-3.1-fast-generate-preview` | VIDEO | STANDARD | Yes | $0.15/sec × 5s = $0.75 |
| `gemini-2.5-pro-preview-tts` | VOICE | STANDARD | Yes | ~$0.02/request (100 words) |

### 10.3 Safety Settings

Use relaxed safety settings for creative content generation:

```typescript
const SAFETY_SETTINGS = [
  { category: 'HARM_CATEGORY_HARASSMENT', threshold: 'BLOCK_ONLY_HIGH' },
  { category: 'HARM_CATEGORY_HATE_SPEECH', threshold: 'BLOCK_ONLY_HIGH' },
  { category: 'HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold: 'BLOCK_ONLY_HIGH' },
  { category: 'HARM_CATEGORY_DANGEROUS_CONTENT', threshold: 'BLOCK_ONLY_HIGH' },
]
```

### 10.4 Agent Constraints

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Tool calls per request | **5** | Prevents runaway tool chains. Covers complex flows (search → think → image) with headroom. |
| Max output tokens (chat) | **2048** | ~200-300 word responses. Long enough for detailed answers, short enough for chat UX. |
| Max output tokens (think) | **8192** | Deep reasoning needs space for chain-of-thought. |
| LLM timeout (chat) | **30s** | Fast enough for conversational UX. |
| LLM timeout (image) | **60s** | Image generation needs more time. |
| LLM timeout (video) | **180s** | Veo is slow by nature. |
