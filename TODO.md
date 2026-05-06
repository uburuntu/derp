# Derp Finance Hardening Harness

Loop: 1 / 5
Status: implementation started

## P0

- [ ] Durable provider-call cost accounting for chat, inline, reminders, and media.
- [ ] Paid routes must use paid/stable provider credentials; free routes cannot satisfy paid promises.
- [ ] Refund debt must be enforceable, visible, and settled by future payments before spendable credits.
- [ ] Payment receipt settlement must be durable and retryable after Telegram payment succeeds.
- [ ] Free usage must have global user quotas and promo-burn visibility.
- [ ] Standard chat must have context/output caps so 2 credits is launch-safe.

## P1

- [ ] High-cost personal spends and all group-pool paid spends require confirmation.
- [ ] Group `/buy` must split personal credits, group pool, and subscriptions.
- [ ] Spend receipts must show personal vs group source, including handled media tools.
- [ ] Admin metrics must show provider cost, gross margin, free burn, debt, unsettled payments, and fallback health.
- [ ] Provider-success / delivery-failure paths must not automatically refund external provider spend.
- [ ] OpenRouter fallback is paid text-only in this PR.

## Loop Notes

- Reviewer roles: finance data model, Telegram commerce UX, provider observability, product economy.
- After each loop, copy new P0/P1 findings here and execute the tractable ones before the next loop.
- Significant tasks should be committed separately.
