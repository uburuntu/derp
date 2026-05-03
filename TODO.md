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
- [x] Loop 2: Re-review after first fixes.
- [x] Loop 3: Re-review after second fixes.
- [x] Loop 4: Re-review after third fixes.
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
- [x] P1 L2-DATA-1: Paid tools must atomically reserve/debit credits before provider work or return unsent media until billing succeeds; current post-success debit can deliver media after concurrent balance races.
- [x] P1 L2-DATA-2: Subscription state must survive refunding a later subscription; replace single user subscription fields as source-of-truth with durable subscription/payment rows or recompute from non-refunded periods.
- [x] P1 L2-DATA-3: Stars payments need durable accounting beyond scrubbed ledger meta: currency, Stars amount, product type/id, target, charge IDs, refund status, and revenue metrics for packs/subscriptions/donations.
- [x] P1 L2-UX-1: Inline mode must not call the LLM on every keystroke while inline updates are rate-limit exempt; restore chosen-result generation or add per-user throttling/cache and credit policy.
- [x] P1 L2-UX-2: Forum-topic reminders must be listed and canceled only within the current topic/general topic.
- [x] P1 L2-OPS-1: Handled failures still become OK spans because `withSpan`/logger middleware overwrite error status; chat/scheduler/refund handled failures need explicit failure recording.
- [x] P1 L2-OPS-2: Scheduler health must fail when processing is wedged; skipped ticks should not refresh successful health.
- [x] P1 L2-OPS-3: CD can leave production stopped if migrations fail after the old container is stopped; migrate before stop or trap rollback.
- [x] P1 L2-OPS-4: Retention must scrub inactive reminder user text/prompt/meta, not only messages and ledger metadata.
- [x] P1 L2-OPS-5: Readiness must verify critical migration/index state, including `ledger_payment_receipt_charge_unique`.
- [x] P1 L3-UX-1: Inline mode must not expose throttle/placeholder copy as selectable inline results while users type.
- [x] P1 L3-UX-2: `/remind list` short IDs must be cancellable, or cancellation must provide inline buttons/full IDs.
- [x] P1 L3-UX-3: Scheduled reminders must not fail permanently because the original setup command was deleted; use actual reply targets only and fallback when Telegram rejects a reply target.
- [x] P1 L3-DATA-1: Successful payment DB application must be separated from post-commit replies/admin notifications/metrics to avoid false manual reconciliation after credits were already applied.
- [x] P1 L3-DATA-2: Subscription payments must not downgrade/shorten the user projection when payment updates arrive out of order; recompute projection from active durable subscription periods.
- [x] P1 L3-CODE-1: Auto-called tool failures must pass sanitized user-facing failure text back to the LLM, not raw internal/provider errors.
- [x] P1 L3-CODE-2: Tool refund failures after provider/tool failure must be isolated, logged, and admin-notified instead of masking the original failure.
- [x] P1 L3-OPS-1: CD must roll back the previous container on new-container start errors, not only failed health checks.
- [x] P1 L3-OPS-2: Reminder delivery failures must make scheduler health fail instead of returning green after marking the row failed.
- [x] P1 L3-OPS-3: Reminder retry must not duplicate sends after Telegram delivery succeeds but DB state update fails.
- [x] P1 L3-OPS-4: Handled failure metrics must avoid high-cardinality IDs while keeping IDs on spans/logs.
- [x] P1 L3-TEST-1: Add Postgres-backed tests for durable payment/refund/quota paths.
- [x] P1 L4-UX-1: Chat-wide sequentialization must not freeze callbacks, inline updates, payments, or other forum topics behind long media requests.
- [x] P1 L4-UX-2: Subscription purchase callbacks must answer immediately and handle invoice-link failures visibly.
- [x] P1 L4-UX-3: `/remind list` and `/remind cancel` must remain free even after reminder creation quota is exhausted.
- [x] P1 L4-DATA-1: Refunded personal credits transferred into chat pools cannot be clawed back; remove the exposed transfer path for launch instead of shipping without credit-lot accounting.
- [x] P1 L4-DATA-2: Payment payloads must bind product, target chat, and target thread so group packs/donations apply to the intended target.
- [x] P1 L4-OPS-1: CD must fail fast rather than starting a second long-polling bot if the old container cannot be renamed/stopped.
- [x] P1 L4-OPS-2: Deploy readiness must require a completed scheduler tick, not only scheduler startup.
- [x] P1 L4-OPS-3: Secret redaction must cover span status/recorded exceptions/handled-failure attributes/fatal logs, not only logger attrs.
- [x] P1 L4-OPS-4: Production must require and validate admin notification delivery for payment/refund reconciliation paths.
- [x] P1 L4-OPS-5: Error boundary must answer callback and pre-checkout updates on exceptions.
- [x] P1 L4-CODE-1: Donation successful-payment DB apply needs payment-failure wrapping and post-commit side-effect isolation.
- [x] P1 L4-CODE-2: LLM reminder paid reservations must refund on provider/send failure and hide raw reservation errors from users.
- [x] P1 L4-CODE-3: Memory must obey tool-command duality; command and LLM tool behavior must not diverge.

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

Status: complete.

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
- Restored atomic paid tool reservations before provider work and refund-on-tool-failure to close concurrent paid media delivery races.
- Added inline answer throttling/cache and non-placeholder copy so inline mode does not call the model for every keystroke.
- Scoped `/reminders`, `/remind list`, and reminder cancellation to the current forum topic or general topic.
- Made handled failure status sticky for tool spans, stopped forcing root update spans to OK, and recorded handled chat/refund/scheduler failures.
- Made scheduler health fail on stale processing and stopped skipped ticks from refreshing success health.
- Moved CD migrations before stopping the running container.
- Extended retention scrubbing to inactive reminder text, prompt, and metadata.
- Added readiness validation for the critical Telegram charge unique index.
- Added durable `payment_receipts` and `subscription_periods` tables with Drizzle migration `drizzle/0003_silent_scarecrow.sql`.
- Routed subscription, pack, donation, and refund flows through durable payment receipts; pack and donation revenue now emits metrics.
- Subscription refunds now mark the refunded period and recompute the active subscription from non-refunded active periods instead of clearing all subscription state.

### Verification

- 2026-05-03: `bun run check` passed after donation parity work.
- 2026-05-03: `bun run check` passed after Loop 2 operational hardening.
- 2026-05-03: `bun run check` passed after durable payment/subscription model.

## Loop 3

Status: complete.

### Reviewers

- Product/Telegram UX reviewer: complete.
- Data model/pricing reviewer: complete.
- Observability/ops reviewer: complete.
- Code quality/regression reviewer: complete.

### P0/P1 Findings

- No P0 findings.
- UX P1: inline typing still produced selectable throttle placeholders; `/remind list` exposed short IDs that `/remind cancel` could not use; reminder delivery could fail if the original reply target was deleted.
- Data/pricing P1: successful payment DB commits shared a `try/catch` with notifications, creating false reconciliation alerts after applied payments; out-of-order subscription payments could shorten the user projection despite durable subscription periods.
- Code P1: auto-called tool failures returned raw error text to the LLM; refund failures after paid tool failure were not isolated/admin-notified; durable credit paths still need Postgres-backed tests.
- Ops P1: deploy rollback did not cover all start-time errors; scheduler health stayed green for terminal reminder delivery failures; reminder retry could duplicate sends after successful Telegram delivery; handled failure metric labels could include high-cardinality IDs.

### Execution

- Changed inline mode to answer empty while typing/throttled and only expose real generated/cached answers as selectable articles.
- Made `/remind cancel` accept unique prefixes from `/remind list`, and stopped storing the setup command as the future reply target; reminders now only use an actual replied-to message.
- Added Telegram reminder delivery fallback: if a stored reply target is unavailable, send in the same topic without replying.
- Split reminder delivery retry from DB state retry so a sent reminder is not sent again when marking delivered fails; terminal delivery/state failures now surface to scheduler health.
- Added delivery metadata with Telegram message IDs on successful reminders.
- Added scheduler failure counters and kept failure IDs out of handled-failure metric labels.
- Added CD `ERR` trap rollback after the previous container is stopped.
- Separated successful payment DB application from post-commit replies/admin notifications/metrics; post-commit failures now report "payment applied, notification failed" to admins only.
- Recomputed subscription projection from durable active subscription periods after subscription payment insert/duplicate handling.
- Returned sanitized tool failure text to the LLM for auto-called tools.
- Isolated tool refund failures with Logfire/admin notification.

### Verification

- 2026-05-03: `ctx7 docs /websites/core_telegram_bots_api ...sendMessage reply_to_message_id...` confirmed Telegram `sendMessage` returns a `Message` and reply-target behavior/fallback options.
- 2026-05-03: `ctx7 docs /grammyjs/website ...answerInlineQuery empty results...` confirmed grammY can answer inline queries with an empty result list and build article results with `InlineQueryResultBuilder`.
- 2026-05-03: `ctx7 docs /drizzle-team/drizzle-orm-docs ...transaction insert update select...` confirmed Drizzle transaction/update/select patterns used for subscription projection.
- 2026-05-03: `bun run check` passed after Loop 3 reliability/UX/payment hardening.

## Loop 4

Status: complete.

### Reviewers

- Product/Telegram UX reviewer: complete.
- Data model/pricing/parity reviewer: complete.
- Observability/ops/security reviewer: complete.
- Code quality/tests/regression reviewer: complete.

### P0/P1 Findings

- No P0 findings.
- UX P1: chat-wide sequentialization could block callbacks behind long `/video`; subscription callbacks created invoice links before answering; reminder list/cancel was quota-gated with create.
- Data/pricing P1: transfers made refund clawback unsound; payment payloads did not bind target chat/thread; DB invariant tests were missing.
- Ops P1: CD could dual-poll if stop/rename failed; readiness could pass before scheduler first tick; span/fatal redaction was incomplete; prod admin notification config was optional; error boundary did not answer callback/pre-checkout failures.
- Code P1: donation payment DB failures lacked reconciliation wrapping; transfer errors could leak raw DB errors; LLM reminder credits were not refunded after provider/send failure; memory violated tool-command duality.

### Execution

- Added explicit CI-gated Postgres integration tests for duplicate Stars charge idempotency, subscription projection/refund recomputation, concurrent free quota reservation, and paid debit/refund idempotency.
- Updated CI to run `bun run check` with the migrated Postgres test database available to the integration suite.
- Narrowed sequentialization to ordinary non-command chat messages by chat/topic; callbacks, inline, pre-checkout, successful payments, and slash commands are no longer queued behind long requests.
- Added compact signed payment payloads for subscriptions, packs, and donations with target chat/thread binding; successful payments now apply group credits and donation receipts to the signed target.
- Removed the exposed personal-to-group credit transfer path for launch instead of shipping refund-unsafe credit movement.
- Answered subscription callbacks before invoice-link creation and added visible invoice-link failure copy.
- Made reminder list/cancel unlimited by moving launch constraints to active/recurring reminder limits rather than daily gating the whole tool.
- Required prod admin IDs/events chat, validated the admin events chat on startup, and made critical reconciliation notifications fail loudly.
- Extended redaction to Brave keys, span statuses, recorded exceptions, handled-failure span attrs, and fatal startup logging.
- Changed scheduler readiness to require a completed successful tick, and hardened CD stop/rename checks to refuse dual long-polling.
- Made the error boundary answer failed callbacks/pre-checkout queries.
- Wrapped donation payment DB apply and post-commit side effects like credit purchases.
- Added idempotent refunds for LLM reminder credit reservations when provider/send delivery fails, with sanitized user skip copy.
- Moved memory slash commands into `memoryTool`, made command/tool memory updates consistently replace memory, and removed the duplicate manual handlers.

### Verification

- 2026-05-03: `ctx7 library Bun ...` and `ctx7 docs /oven-sh/bun ...skipIf...` confirmed `describe.skipIf`/`test.skipIf` patterns for conditional integration tests.
- 2026-05-03: `ctx7 docs /websites/core_telegram_bots_api ...answerCallbackQuery answerPreCheckoutQuery...` confirmed callback answers remove client wait state and pre-checkout must be answered within 10 seconds.
- 2026-05-03: `ctx7 docs /websites/core_telegram_bots_api ...getChat...` confirmed startup validation can use `getChat` for the admin events chat.
- 2026-05-03: `ctx7 docs /grammyjs/website ...error handling callback query...` confirmed grammY error handling and callback answer patterns.
- 2026-05-03: `ctx7 docs /grammyjs/website ...runner sequentialize...` confirmed narrowing `sequentialize` constraints allows non-colliding updates to run concurrently.
- 2026-05-03: `ctx7 docs /open-telemetry/opentelemetry-js ...recordException setStatus...` confirmed span status/exception APIs used for redacted errors.
- 2026-05-03: `bun run check` passed locally with Postgres integration tests intentionally skipped unless `DERP_RUN_DB_TESTS=1`.
- 2026-05-03: Local Docker-backed integration run could not start because the Docker daemon is not running; CI will execute these tests with its Postgres service.
- 2026-05-03: `bun run check` passed after Loop 4 UX/ops/payment hardening.
