# Derp Finance Notes

This file records the launch economics assumptions behind credits, subscriptions, paid tools, and provider routing. If provider prices or Telegram Star payout changes, update this file and the economy tests in the same change.

## Credit Floor

Telegram Stars are treated as `0.013 USD` of developer payout per Star. Derp sells discounted credits, so pricing must use the cheapest sold credit, not the one-Star-per-credit packs.

Current cheapest credit:

- Ultra subscription: `1500 Stars -> 2500 credits`.
- Floor: `1500 * 0.013 / 2500 = 0.0078 USD/credit`.

With a 30% gross-margin target, each credit should cover no more than about `0.00546 USD` of provider cost.

## Current Policy

- Subscriptions grant monthly personal credits.
- Top-up packs grant one-time personal or group credits.
- Standard chat costs 2 credits per normal chat turn.
- If neither the user nor chat pool can pay for Standard chat, Derp falls back to the Free model.
- Active subscription status does not unlock unlimited paid-model chat after credits are spent.
- Image and edit tools have no free generation quota for launch.
- Paid tool pricing is checked against the cheapest sold credit floor in unit tests.

## Provider Takeaways

Google Gemini pricing checked with `tvly` and Context7:

- Gemini 2.5 Flash: approximately `$0.30/M` input and `$2.50/M` output tokens.
- Gemini 2.5 Flash-Lite: approximately `$0.10/M` input and `$0.40/M` output tokens.
- Gemini 3.1 Pro Preview: approximately `$2/M` input and `$12/M` output tokens up to 200k prompts.
- Gemini 2.5 Flash Image: approximately `$0.039` per image.
- Gemini 2.5 Flash TTS: approximately `$0.50/M` input and `$10/M` output audio tokens.
- Veo 3.1 Fast: approximately `$0.10/sec` at 720p, `$0.12/sec` at 1080p, `$0.30/sec` at 4K.

Media prices are generally safe when paid. The dangerous cases are unmetered text, long `/think` outputs, long TTS, free generation quotas, refunds after users already spent credits, and provider-success/user-delivery-failure paths.

## OpenRouter Policy

OpenRouter free routes are useful for best-effort Free-tier fallback, not for paid promises.

Observed limits and risks:

- Free models use `:free` IDs or `openrouter/free`.
- Free tier is rate limited, commonly 50 requests/day without paid credits and 20 requests/minute.
- Accounts with enough purchased OpenRouter credits can get a higher free-model daily cap, but availability and quality remain variable.
- Free model availability can change, and failed attempts may still count against quota.
- Paid OpenRouter routing can be useful for provider fallback if actual model, cost, and usage are recorded.

Derp should not satisfy paid tools with free OpenRouter models. Use free OpenRouter only behind strict quotas and with visible degradation.

## Remaining Finance Risks

- No durable provider-call cost table yet. Admin metrics estimate revenue and liabilities from internal credits, but not actual provider bills.
- Refund debt is visible in admin metrics, but there is not yet a user-facing debt/repayment flow.
- High-cost/group-pool tool usage does not require an extra confirmation screen.
- Free quota accounting is still per user, per chat, per day; global user-level promo budgets would be safer for public launch.
