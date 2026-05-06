# Derp Finance Hardening Harness

Loop: 5 / 5
Status: loop 5 remediation complete

## P0

- [x] Durable provider-call cost accounting for chat, inline, reminders, and media.
- [x] Paid routes must use paid/stable provider credentials; free routes cannot satisfy paid promises.
- [x] Refund debt must be enforceable, visible, and settled by future payments before spendable credits.
- [x] Payment receipt settlement must be durable and retryable after Telegram payment succeeds.
- [x] Free usage must have global user quotas and promo-burn visibility.
- [x] Standard chat must have context/output caps so 2 credits is launch-safe.

## P1

- [x] High-cost personal spends and all group-pool paid spends require confirmation.
- [x] Group `/buy` must split personal credits, group pool, and subscriptions.
- [x] Spend receipts must show personal vs group source, including handled media tools.
- [x] Admin metrics must show provider cost, gross margin, free burn, debt, unsettled payments, and fallback health.
- [x] Provider-success / delivery-failure paths must not automatically refund external provider spend.
- [x] OpenRouter fallback is paid text-only in this PR.

## Loop 1 Reviewer P1 Findings

- [x] Refunds must reverse the full purchased credits, including credits that settled prior refund debt.
- [ ] Paid reservations are final spends with no recoverable `reserved/succeeded/refunded` state.
- [x] Confirmed tool spend must not charge a different account/source than the confirmation showed.
- [x] Wrong users must not be able to expire/cancel another user's group confirmation message.
- [x] Admin-initiated refunds must not suppress the user-facing refund/debt explanation.
- [x] Financial payment/refund outcome copy must use i18n, not hardcoded English.
- [x] LLM reminders must create provider-call accounting rows.
- [x] Fallback routing must be visible in admin metrics and message metadata.
- [x] Provider-success / Telegram-delivery-failure paths must not auto-refund provider spend.

## Loop 2 Reviewer P1 Findings

- [x] Refund of an unsettled receipt must not create false refund debt.
- [x] Debt blocking must be enforced atomically with paid deduction.
- [x] Free chat turns must be durably idempotent.
- [x] Standard chat debit must be inside the refund-protected execution path.
- [x] Chat-pack payment failures must persist a receipt before internal chat lookup can fail.
- [x] Personal payment confirmations must not be posted back to a public/group payload chat.
- [x] Pack invoice creation failures must use localized user-facing error handling.
- [x] Pay-critical invoice and validation copy must be localized.
- [x] Unsettled payment visibility must include charge IDs and retry commands.
- [x] Provider metrics must include media/fallback paths.
- [x] Google video calls must persist operation IDs.

## Loop 3 Reviewer P1 Findings

- [x] Generated-video provider success must stay billable if Telegram download fails.
- [x] High-cost tool outputs must link persisted messages to provider-call rows and cost.
- [x] `/info` must expose provider route, call IDs, and provider cost for debugging.
- [x] Admin metrics must show fallback health outside the top-provider rollup.
- [x] Admins need a stale-provider-call view with user/chat/request IDs.
- [x] Personal pack/subscription payments must not be exposed in group chats.
- [x] Payment validation, debt, and refund-reconciliation user copy must use i18n.
- [x] Payment settlement failure must not overwrite refunded/settled terminal state.
- [x] Chat context must be capped enough that flat Standard pricing cannot explode on large history.
- [x] Failed multi-turn chat provider calls must preserve partial usage/cost in provider accounting.

## Loop 4 Reviewer P1 Findings

- [x] OpenRouter paid fallback must be model-allowlisted and alert if actual cost exceeds the Standard chat cap.
- [x] Free chat/search needs bot-wide daily caps and operator-visible usage/limits.
- [x] Captured but invalid credit payments must create unsettled receipts for admin recovery.
- [x] Donation payments must not disappear when the target chat row is missing.
- [x] Chat-pack settlement retry must recover the target chat from receipt metadata.
- [x] Paid-but-undelivered provider work must send critical admin notifications.
- [x] `/credits`, confirmations, receipts, and footers must not expose personal balances in groups.
- [x] Forwarded group-pack invoices must not keep a payable forwarded copy.
- [x] Open group debt must not block debt-free personal paid spend.
- [x] Paid spends must link ledger rows to provider-call rows and carry lifecycle status in ledger metadata.

## Loop 5 Reviewer P1 Findings

- [x] Paid reservations must expose a recoverable lifecycle and FK link provider calls to the spend ledger row.
- [x] Paid text-tool delivery failures must notify admins and mark spend delivery failure.
- [x] Personal refund-debt amounts must not leak in group error messages.
- [x] Billable confirmation failures must not claim credits were refunded.
- [x] Payment settlement-failure marking must happen before user-message delivery and manual retry errors must update receipt diagnostics.
- [x] Invoice callback acknowledgement failures must not be treated as invoice delivery failures.
- [x] Billable LLM reminder delivery failures must notify admins with provider call IDs/cost.
- [x] `/admin metrics` must show today's bot-wide free usage against daily caps.
- [x] Fallback provider metrics/admin rollups must attribute actual model, not only requested model.

## Follow-up Debt

- [ ] A future schema migration should promote spend lifecycle metadata into typed columns/table if this becomes an operator workflow.

## Platformization Debt

- [x] Split tool execution-context construction and delivery persistence out of `src/tools/registry.ts`.
- [x] Split credit ledger/balance mutation and free-quota accounting out of `src/db/queries/credits.ts`.
- [x] Split admin metrics and standalone refund command out of `src/handlers/admin.ts`.
- [ ] Split remaining `src/db/queries/credits.ts` payment-settlement, subscription, donation, and refund-reconciliation repositories.
- [ ] Split remaining `src/handlers/admin.ts` finance recovery views, DB maintenance, and smoke-test actions.
- [ ] Split `src/handlers/credits.ts` into balance view, purchase flows, receipts, refunds, and group-pool UX.
- [ ] Continue shrinking provider adapters by moving shared Google media-operation orchestration out of `src/llm/providers/google.ts`.

Current largest files after this pass: `src/llm/providers/google.ts` (1073), `src/handlers/credits.ts` (1068), `src/db/queries/credits.ts` (995), `src/handlers/chat.ts` (838), `src/handlers/settings.ts` (755), `src/db/queries/finance.ts` (747), `src/handlers/admin.ts` (723).

## Loop Notes

- Reviewer roles: finance data model, Telegram commerce UX, provider observability, product economy.
- After each loop, copy new P0/P1 findings here and execute the tractable ones before the next loop.
- Significant tasks should be committed separately.
