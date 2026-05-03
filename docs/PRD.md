# Derp Product Requirements

This page is the repo-local source of truth for Derp product behavior. If implementation, README copy, or agent guidance conflict with it, update this page first or reject the conflicting change.

## Purpose

Use this page to decide whether a change keeps Derp coherent before the bot grows into more levels, tools, or paid features. It gives maintainers, reviewers, and agents the same plain-language contract: what Derp promises users, what is deliberately constrained, and which decisions are still open.

If this page also attracts search traffic, its job is still practical. A prospective user or contributor should be able to understand what Derp does, how credits work, and which actions require explicit user intent.

## Product Promise

Derp is a Telegram AI assistant for private chats and groups. It answers in the chat where it is invoked, can use safe automatic tools for low-risk lookups, and exposes paid or persistent actions through explicit slash commands or confirmation flows.

Users need Derp to be:

- Useful in normal Telegram conversations without leaving the chat.
- Predictable about what costs credits and what is free.
- Safe in groups, especially around memory, reminders, and forum topics.
- Clear about which features are automatic and which require commands.

## Platform Contract

- Runtime: Bun.
- Bot framework: grammY.
- Database: PostgreSQL with Drizzle migrations in `drizzle/`.
- LLM provider: Google GenAI SDK.
- User-facing system messages: Telegram HTML with `parse_mode: "HTML"`.

## Tiers

- `FREE`: default tier for users and chats without paid balance or active subscription.
- `STANDARD`: available when the user or chat has a positive credit balance, or the user has an active non-expired subscription.
- `PREMIUM`: model registry/catalogue only. The orchestrator must not return it.

Normal chat turns currently do not spend credits. A positive balance unlocks `STANDARD` chat while the balance remains positive.

## Credits And Packs

Subscriptions:

- Lite: 150 Stars, 200 credits/month.
- Pro: 500 Stars, 750 credits/month.
- Ultra: 1500 Stars, 2500 credits/month.

Top-up packs:

- Micro: 50 Stars, 50 credits.
- Small: 150 Stars, 150 credits.
- Medium: 500 Stars, 550 credits.
- Large: 1500 Stars, 1800 credits.

Free daily tool quotas are currently tracked per user, per chat, per day.

## Tool Policy

Tools that spend credits, generate media, write memory, or create reminders require explicit slash command intent or a confirmation flow.

Tools may be exposed for model-initiated automatic calls only when they are marked `allowAutoCall` and are safe without confirmation.

Current automatic tools:

- Web search.
- Scoped member profile photo lookup.

## Reminders

- Plain one-time and recurring reminders are supported through `/remind`.
- Reminder storage and execution support one-time LLM reminders, but no user-facing creation flow is enabled yet.
- LLM reminder execution currently deducts 1 credit before calling the model.
- Recurring LLM reminders are disabled until lifetime limits, per-fire pricing, and cancellation UX are defined.

## Data Boundaries

- Forum topic context should stay inside its topic.
- General chat context should not include forum-topic messages by default.
- Stored memory and Telegram/user-provided text must be escaped before rendering as Telegram HTML.

## Open Decisions

- Chat turn metering: if normal chat turns should spend credits, define the per-turn cost here before implementation.
- Free quota scope: if quotas should become global per user per day, add a migration and update help copy in the same change.
- Retention: define retention windows for message text, file IDs, chat memory, custom instructions, and ledger metadata.
- LLM reminders: define creation UX, lifetime limits, retry/refund behavior, and cancellation rules before exposing them to users.
