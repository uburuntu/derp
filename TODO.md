# Derp Review Loop TODO

This file is the persistent harness for the five-loop hardening pass. If the session fails, resume from the latest loop section with unchecked P0/P1 items.

## Goals

- Better Telegram UX and aesthetics: clear HTML messages, simple usage, useful menus, strong onboarding.
- Strong data model: explicit invariants, migration safety, auditability, retention decisions.
- Reasonable pricing: easy to understand, monitor, adjust, and refund/reconcile.
- Excellent observability/debuggability: Logfire spans, metrics, admin diagnostics, actionable errors.
- TypeScript version should be functionally superior to the old Python Derp.
- No existing user base: breaking schema/product changes are allowed if they make the launch version better.
- Telegram UX is open-ended: add/change commands, callbacks, buttons, menus, and other Telegram-native capabilities when useful.

## Loop Status

- [x] Loop 1: Initial cross-functional review and fixes.
- [ ] Loop 2: Re-review after first fixes. In progress.
- [ ] Loop 3: Re-review after second fixes.
- [ ] Loop 4: Re-review after third fixes.
- [ ] Loop 5: Final hardening review and fixes.

## Current Verification

- 2026-05-03 before loop start: `bun run check` passed locally and GitHub CI passed on PR #28.

## P0/P1 Queue

Items below are added by reviewer loop. Keep P0/P1 only here; P2+ notes can stay in loop notes.

- [x] P1 L1-UX-1: Propagate forum `message_thread_id` and reply targets for tool sends, invoices, settings/buy follow-ups, and callback-driven messages.
- [x] P1 L1-UX-2: Do not silently rate-limit callback/inline updates; every callback must be answered.
- [x] P1 L1-UX-3: Fix `/search` quota behavior so credits either unlock overage or rejection copy does not point to `/buy`.
- [x] P1 L1-UX-4: Hide internal/provider error strings from user-facing replies; log raw errors separately.
- [x] P1 L1-UX-5: Make inline mode resilient so it does not leave permanent `Thinking...` messages.
- [x] P1 L1-DATA-1: Add uniqueness/invariants for Telegram payment charge IDs used as credit receipts.
- [x] P1 L1-DATA-2: Make free daily quota reservation atomic under concurrency.
- [x] P1 L1-DATA-3: Replace pre-execution paid credit spend with a safer reservation/settlement model or post-success debit.
- [x] P1 L1-DATA-4: Define and implement retention/scrubbing for sensitive Telegram text/file IDs/metadata.
- [x] P1 L1-OPS-1: Make `/health` reflect live bot and scheduler state, not only latched startup flags.
- [x] P1 L1-OPS-2: Deploy by immutable image digest, not mutable `latest`.
- [x] P1 L1-OPS-3: Add Logfire/console safety so production logging cannot be silently absent.
- [x] P1 L1-OPS-4: Mark handled chat/tool failures as observable failures with counters/spans.
- [x] P1 L1-PRICE-1: Remove welcome credit permanent `STANDARD` unlock or split promo credits from paid unlock credits.
- [x] P1 L1-PRICE-2: Wrap successful payment processing in local failure handling with admin reconciliation details.
- [x] P1 L1-PRICE-3: Add refund reconciliation path for already-refunded Telegram charges.
- [ ] P1 L2-DATA-1: Paid tools must atomically reserve/debit credits before provider work or return unsent media until billing succeeds; current post-success debit can deliver media after concurrent balance races.
- [ ] P1 L2-DATA-2: Subscription state must survive refunding a later subscription; replace single user subscription fields as source-of-truth with durable subscription/payment rows or recompute from non-refunded periods.
- [ ] P1 L2-DATA-3: Stars payments need durable accounting beyond scrubbed ledger meta: currency, Stars amount, product type/id, target, charge IDs, refund status, and revenue metrics for packs/subscriptions/donations.
- [ ] P1 L2-UX-1: Inline mode must not call the LLM on every keystroke while inline updates are rate-limit exempt; restore chosen-result generation or add per-user throttling/cache and credit policy.
- [ ] P1 L2-UX-2: Forum-topic reminders must be listed and canceled only within the current topic/general topic.
- [ ] P1 L2-OPS-1: Handled failures still become OK spans because `withSpan`/logger middleware overwrite error status; chat/scheduler/refund handled failures need explicit failure recording.
- [ ] P1 L2-OPS-2: Scheduler health must fail when processing is wedged; skipped ticks should not refresh successful health.
- [ ] P1 L2-OPS-3: CD can leave production stopped if migrations fail after the old container is stopped; migrate before stop or trap rollback.
- [ ] P1 L2-OPS-4: Retention must scrub inactive reminder user text/prompt/meta, not only messages and ledger metadata.
- [ ] P1 L2-OPS-5: Readiness must verify critical migration/index state, including `ledger_payment_receipt_charge_unique`.

## Loop 1

Status: complete.

### Reviewers

- Product/Telegram UX reviewer: complete.
- Data model/migrations reviewer: complete.
- Pricing/credits reviewer: complete.
- Observability/debuggability reviewer: complete.

### P0/P1 Findings

- No P0 findings.
- UX P1: forum topic leakage, callback rate-limit spinners, misleading search quota upsell, internal error strings, fragile inline placeholder flow.
- Data P1: duplicate payment receipt risk, non-atomic free quotas, pre-execution spend without durable outcome, missing retention/scrubbing.
- Data non-applicable for launch: Alembic-to-Drizzle bridge migration. There is no existing user base, so fresh schema is acceptable.
- Ops P1: latched health, mutable `latest` deploy, possible silent production logging, handled errors not reflected as failures.
- Pricing P1: welcome credits permanently unlock `STANDARD`, payment processing lacks local failure handling, refunds need reconcile-only path.

### Execution

- Removed automatic welcome credits so `STANDARD` is not unlocked by a free promo.
- Made tool billing safer: free quota is reserved atomically before provider work, paid credits are debited only after successful tool execution, and duplicate idempotency keys are detected without double charge.
- Added a partial unique index for paid Telegram charge receipts and generated migration `drizzle/0002_common_photon.sql`.
- Added retention scrubbing for message text/file IDs/generation metadata after 30 days and ledger metadata after 400 days; PRD now states the policy.
- Added live bot runner and scheduler health checks, scheduler retention job state, and shutdown readiness clearing.
- Changed CD deploys to use the immutable image digest.
- Required `LOGFIRE_TOKEN` in prod and added handled failure metrics/spans.
- Propagated forum topic IDs through chat replies, tool sends, settings/buy follow-ups, invoices, reminders/help/info, and command output.
- Excluded callback and inline updates from incoming rate limiting.
- Reworked inline mode to answer with complete article text instead of a permanent `Thinking...` placeholder.
- Wrapped successful payment processing with admin reconciliation details and added local refund reconciliation for already-refunded charges.

### Verification

- 2026-05-03: `ctx7 docs /websites/core_telegram_bots_api ...inline...` confirmed `answerCallbackQuery` removes client spinners and `answerInlineQuery` sends complete article results.
- 2026-05-03: `ctx7 docs /websites/core_telegram_bots_api ...payments...` confirmed `sendInvoice.message_thread_id`, `refundStarPayment`, and Stars payment fields.
- 2026-05-03: `ctx7 docs /grammyjs/website ...inline/callback...` confirmed grammY inline query and callback handling patterns.
- 2026-05-03: `bun run check` passed.

## Loop 2

Status: in progress.

### Reviewers

- Product/Telegram UX reviewer: complete.
- Data model/pricing reviewer: complete.
- Observability/ops reviewer: complete.
- Code quality/regression reviewer: complete.

### P0/P1 Findings

- No P0 findings.
- Data/pricing P1: paid tool billing lacks atomic reservation, subscription refunds can erase a previous active subscription, and Stars payment accounting is not durable enough for launch.
- UX P1: inline mode now spends model quota on every inline keystroke; forum-topic reminders still list/cancel across topics.
- Ops P1: handled failures can still look OK, scheduler health can stay green while processing is wedged, CD migration failure can leave prod stopped, retention misses reminders, and readiness does not validate the critical payment index.
- Code regression P1: donation callback typecheck issue was fixed before commit.

### Execution

- Audited old Python `origin/main` functionality and restored `/donate` and `/support` Stars donation flow in TypeScript with Telegram invoices, callbacks, localized copy, topic-aware invoice replies, and admin notifications.

### Verification

- 2026-05-03: `bun run check` passed after donation parity work.
