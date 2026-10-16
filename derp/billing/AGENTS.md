# Billing — Stars payments

- Owns `purchases`: one receipt per successful Stars payment (pack or subscription cycle), unique on `telegram_charge_id`, never deleted; `status` flips to `refunded`.
- `purchases.py`: the invoice payload `p1:<product>:<credits>:<user|chat>:<id>` names exactly what was offered and whose wallet gets it. Pre-checkout (`refusal`) accepts only offers that still match `Settings.packs` (versioned ids) in XTR, and personal invoices only from the person they were made for; chat invoices anyone may pay. `record_payment` writes the receipt, then credits a `purchase` lot with operation `stars:<charge id>`, in one transaction — a redelivered payment credits nothing.
- `command.py`: `/buy` (private → personal packs; group → "for me / for this chat" first), pack buttons post a Stars invoice, `pre_checkout_query` and `successful_payment` handlers. Thanks copy is plain; each purchase pings the owners.
- The billing router sits before the agent handler: payment service messages never invoke Derp.
- Money copy stays plain (no persona); invoices mention /terms.
