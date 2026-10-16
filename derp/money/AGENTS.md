# Money — wallets and the ledger

- Owns `wallets` (one per user and per chat), `credit_lots`, `reservations` + `reservation_holds` and `ledger_entries`. Amounts are integer Derpcoins; `Settings.credits_for_usd` converts provider cost.
- A balance is the sum of unexpired lots' `remaining`. Lot sources: `plan` (cycle allowance, expires), `purchase`, `sponsored` (a day's allowance, expires), `grant`, `migration`. Lots are spent soonest-expiring first, then oldest.
- `wallets.py` is the only writer. Every movement locks the wallet row first (serial per wallet, so never negative) and is keyed by a caller-chosen operation ID (`run:<id>`, `stars:<charge id>`…); repeating an operation returns the first result.
- Flow: `reserve` (full amount, or at least `minimum`, from chosen sources) → `capture(actual)` spends from the holds and returns the rest; the charge never exceeds the hold, the difference is eaten, never owed. Returning credits to a lot that expired meanwhile expires them. `release` = capture 0; `release_stale` frees holds of crashed processes.
- `expire_lots` and `claw_back` (refunds: takes only what is left of a lot; spent credits are not recovered) write their own entries.
- DB guarantees: `CHECK 0 ≤ remaining ≤ amount`, captured ≤ held, ledger entries positive with unique (operation, kind, lot), and a trigger rejecting UPDATE/DELETE on `ledger_entries`.
- Invariant (tested): per lot, `amount − remaining = hold − release + expire + clawback`; `capture` entries record spending and do not change the lot.
- All functions run inside the caller's transaction; never hold one across Telegram or provider calls.
