# Derp Finance Hardening Harness

Loop: 2 / 5
Status: reviewer loop 2 findings captured; remediation in progress

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

## Loop Notes

- Reviewer roles: finance data model, Telegram commerce UX, provider observability, product economy.
- After each loop, copy new P0/P1 findings here and execute the tractable ones before the next loop.
- Significant tasks should be committed separately.
