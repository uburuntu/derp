# Telegram operator console

The operator console is Derp's private deployment-control surface. An
**operator** is a Telegram user explicitly trusted by the deployment. This is
not a Telegram chat role: group administrators and chat owners gain no operator
access, and operators gain no chat-administrator privileges.

## Configure access

Set the canonical deployment allowlist in `.env.prod`:

```dotenv
OPERATOR_IDS=[123456789,987654321]
```

A comma-separated value is also accepted. IDs must be positive integers, and
production refuses to start with an empty allowlist. `ADMIN_IDS` remains a
deprecated migration alias; use `OPERATOR_IDS` for every new deployment and do
not configure both names.

Each configured ID receives a complete, localized, chat-specific Telegram
command scope containing `/operator`. `/ops` is an unlisted alias. Access is
checked again inside the bot through the injected immutable allowlist; the
command menu is discovery, not authorization.

The console opens only in a direct private chat where the chat ID matches the
operator's user ID. Group use is redirected to private chat. Unauthorized
commands and callbacks are consumed before conversational routes and reveal no
allowlist details. Console panels use Telegram's protected-content flag.

## What it shows

Every database snapshot is aggregate-only and uses identifier-free typed
values. A failed snapshot becomes explicitly `degraded`; runtime status remains
available.

| View | Data |
| --- | --- |
| Overview | Environment, process uptime, database readiness/latency, running workers, and an aggregate attention count |
| Usage | User/chat totals and rows changed in 24 hours, retained-message totals, chat-type counts, and artifact count/bytes |
| Commerce | Canonical available/reserved/consumed/debt credits, fulfilled/clawed-back Stars, entitlement/renewal counts, and purchase/receipt states |
| Operations | Paid-operation, delivery, and deferred-approval state counts |
| Runtime | Derp/Python/library versions, purchase and AI-content-export flags, database-pool gauges, worker state, and the verified model catalog |
| Tests | Database-read status, the live 1-Star checkout, and command-menu reconciliation |

The attention count combines review-required purchases/receipts, failed paid
operations, failed or uncertain deliveries, wallet debt presence, unavailable
database diagnostics, and stopped workers. It is a triage signal, not an alert
history.

Diagnostic snapshots never expose message or prompt content, tool arguments,
callback or payment payloads, row-level user/chat/payment identifiers, invoice
or charge IDs, artifact paths or content bytes, signed URLs, tokens, secrets, raw
environment variables, or arbitrary database queries. The console also has no
row drill-down, shell, direct balance mutation, refund control, or generic SQL
control.

## Maintenance

Maintenance invokes the exact worker instances already owned by the running
application. Requests are serialized within the process and accept no
operator-supplied arguments.

| Action | Live pass |
| --- | --- |
| History | Purge history past its retention boundary |
| Subscriptions | Expire completed subscription cycles |
| Operations | Reconcile quotes, reservations, executions, and durable delivery recovery |
| Deliveries | Reconcile interrupted/retryable deliveries and expire artifacts |
| Approvals | Expire deferred tool approvals |
| All passes | Run the five passes above in that order |

Every action first issues a single-use token bound to the operator and exact
action. The default lifetime is two minutes. A token mismatch, reuse, expiry,
or process restart fails closed.

The result says **pass completed**, not **succeeded**. Completion means the
worker's `sweep()` call returned. Some workers deliberately contain per-item
failures and expose them through result counters, so review non-zero failure or
uncertain counts and telemetry before retrying. An exception that escapes a
worker is reported as an interrupted pass.

## Testing and command sync

`Open 1-Star checkout` uses the durable production purchase-intent, Telegram
invoice, settlement, and wallet-credit path. It is not a mock or sandbox: an
operator who confirms Telegram's final payment prompt spends one real Star.
The hidden product remains available through `/debug_buy` for compatibility;
retired legacy debug commands redirect to `/operator`.

`Sync command menu` reapplies the complete desired command state for every
supported locale and audience, including every operator's private chat scope.
It is idempotent and also removes the default scope, matching startup behavior.

## Observability

Successful operator actions emit:

- `operator.maintenance_pass_completed`
- `operator.command_menu_sync_completed`
- `operator.debug_purchase_intent_presented`
- `telegram.command_menu_configured`

Degraded or failed boundaries use privacy-safe `report_exception()` events:

- `operator.snapshot_degraded`
- `operator.panel_open_failed`, `operator.panel_refresh_failed`, and
  `operator.panel_edit_failed`
- `operator.maintenance_confirmation_render_failed`,
  `operator.maintenance_pass_failed`, and
  `operator.maintenance_result_render_failed`
- `operator.test_purchase_open_failed` and
  `operator.debug_purchase_invoice_link_failed`
- `operator.command_menu_sync_failed` and
  `operator.command_menu_result_render_failed`

Telemetry may include bounded action/count/timing fields and correlation IDs.
Never add console content, callbacks, confirmation tokens, payment payloads, or
secrets to these events.

## Operational limits

- Confirmation tokens are bounded, in-memory, process-local capabilities. They
  expire after two minutes by default and are discarded on restart.
- Snapshots are live reads, not stored time series. User and chat 24-hour values
  mean rows changed by their `updated_at` timestamps; retained messages use the
  Telegram message timestamp.
- A database-query failure degrades the database snapshot as a unit. Use
  telemetry for the cause; the console intentionally does not return exception
  details.
- Maintenance serialization is process-local. Production's single-poller
  invariant remains required.
- Protected Telegram content reduces accidental forwarding; it is not a
  substitute for protecting the operator's Telegram account and device.

## Extend or remove it

Add diagnostics by extending the identifier-free values in
`derp/operator/types.py`, aggregate queries in `derp/operator/service.py`, and
bounded presentation in `derp/handlers/operator.py`. Add tests that prove no
row identifiers or content cross the service boundary.

Add maintenance only when an existing bounded, retry-safe domain worker owns the
operation. Inject its live runtime instance, expose a fixed enum action, require
confirmation, and return aggregate counters. Do not put ad hoc mutations or SQL
inside the Telegram handler.

Keep `operator.router` and `operator.rejection_router` before debug and chat
routes. Add route-scoped dependencies only when a control requires them; the
current operator callback plan loads database models solely for the 1-Star test.
Keep callbacks actor-bound and private-chat-bound.

When deleting or renaming a control, first remove it from the desired command
menu and resync every scope. Retain a fail-closed rejection for stale commands
or callback prefixes for a compatibility window, update the history exclusion
policy, then remove dependencies and presentation code. The console invokes
maintenance workers but does not own their lifecycle, so removing a button must
not remove a worker still required for background recovery.
