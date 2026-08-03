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
| Commerce | Canonical available/reserved/consumed/debt credits, fulfilled/clawed-back Stars, entitlement counts, durable renewal/payment/refund queue health, purchase/receipt states, and content-free support totals |
| Support | Paginated active cases with reference, category, creation time, requester Telegram ID, one bounded user note, and an optional exact payment receipt |
| Operations | Paid-operation, delivery, and deferred-approval state counts |
| Inference | Aggregate attempts and states, tokens by category, reconciled and pending OpenRouter cost, reviewed role availability, and cached live key/catalog status |
| Runtime | Derp/Python/library versions, purchase and AI-content-export flags, database-pool gauges, worker state, and the verified model catalog |
| Tests | Database-read status, the live 1-Star checkout/refund, and command-menu reconciliation |

The attention count combines review-required purchases/receipts, failed paid
operations, failed or uncertain deliveries, open support cases, wallet debt
presence, unavailable database diagnostics, and stopped workers. It is a triage
signal, not an alert history.

Each new `/support` case also sends configured operators a best-effort protected
notice containing only its category and opaque reference. The durable,
eight-cases-per-page Support view is authoritative and remains usable if that
notice fails. Opening a case reveals its bounded user note and, for payment or
refund cases, the exact requester-owned receipt facts needed for a decision.

**Reply and close** and **Decline** collect one bounded operator message through
a private ForceReply. Derp stores the exact status and reason, then edits the
requester's original stable case message in place. **Refund** is available only
when the case is bound to a receipt; it submits that exact receipt through the
restart-safe refund service and edits the same message to the completed or
provider-pending state. Superseded confirmation callbacks are inert.

The user note and operator decision text are purged 30 days after closure. The
reference, category, status, requester/payment relations, timestamps, and
accounting records remain for idempotency and dispute handling.

Diagnostic snapshots never expose message or prompt content, tool arguments,
callback or payment payloads, row-level chat/payment identifiers, invoice or
charge IDs, artifact paths or content bytes, signed URLs, tokens, secrets, raw
environment variables, or arbitrary database queries. The scoped Support detail
view is the deliberate exception: allowlisted operators see the requester
Telegram ID, one bounded note, and bounded facts for the receipt already selected
by that requester. The console otherwise has no shell, direct balance mutation,
operator-entered payment target, arbitrary refund, or generic SQL control.

## Maintenance

Maintenance invokes the exact worker instances already owned by the running
application. Requests are serialized within the process and accept no
operator-supplied arguments.

Runtime reports `Payments` as on only when both the durable payment-update
replay worker and operator-test refund reconciler are running. A manual payment
pass returns separate aggregate counters for update settlement, user replies,
and test-refund recovery. The same live refund worker also completes reconciled
support refunds and purges closed-case note/reason text after 30 days.

| Action | Live pass |
| --- | --- |
| History | Purge history past its retention boundary |
| Subscriptions | Expire completed subscription cycles and replay durable renewal commands |
| Payments | Replay durable Telegram payment updates, reconcile refunds, complete support refund states, and purge expired support text |
| Operations | Reconcile quotes, reservations, executions, and durable delivery recovery |
| Deliveries | Reconcile interrupted/retryable deliveries and expire artifacts |
| Approvals | Expire deferred tool approvals |
| Inference | Reconcile bounded OpenRouter usage/cost records without replaying provider work |
| All passes | Run the seven passes above in that order |

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
The hidden product remains available through private `/debug_buy` for
compatibility; retired legacy debug commands redirect to `/operator`.

`Refund latest 1-Star test` requires a second single-use confirmation. The bot
selects only that operator's latest fulfilled debug product and calls Telegram's
Stars refund API with its stored charge. Telegram's normal refund update then
drives the existing idempotent receipt clawback; the control does not mutate a
wallet directly and cannot target public products. This diagnostic is separate
from the Support view's requester-selected public-receipt refund action.

`Run read-only check` on the inference page reads only OpenRouter key spending
limits and the live model catalog. It sends no prompt and performs no inference.
The page also exposes aggregate recorded tokens, reconciliation state, and
actual cost without user, chat, request, or provider-response identifiers.

`Sync command menu` reapplies the complete desired command state for every
supported locale and current audience, including each configured operator's
private chat scope. It is idempotent and also removes the default scope,
matching startup behavior.

## Observability

Successful operator actions emit:

- `operator.maintenance_pass_completed`
- `operator.command_menu_sync_completed`
- `operator.debug_purchase_intent_presented`
- `operator.test_refund_requested`
- `operator.inference_read_check_completed`
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
- `operator.test_refund_confirmation_render_failed`,
  `operator.test_refund_failed`, and `operator.test_refund_result_render_failed`
- `operator.inference_read_check_failed` and
  `operator.inference_check_result_render_failed`
- `operator.command_menu_sync_failed` and
  `operator.command_menu_result_render_failed`
- `operator.support_case_read_failed`,
  `operator.support_confirmation_render_failed`,
  `operator.support_status_edit_failed`,
  `operator.support_queue_refresh_failed`, and
  `operator.support_queue_render_failed`

Telemetry may include bounded action/count/timing fields and correlation IDs.
Never add console content, callbacks, confirmation tokens, payment payloads, or
secrets to these events.

## Operational limits

- Confirmation tokens are bounded, in-memory, process-local capabilities. They
  expire after two minutes by default and are discarded on restart.
- Snapshots are live reads, not stored time series. User and chat 24-hour values
  mean rows changed by their `updated_at` timestamps; retained messages use the
  Telegram message timestamp.
- OpenRouter balance/catalog results are process-local cached metadata. A failed
  probe is explicit and never changes catalog enablement or sends inference.
- A database-query failure degrades the database snapshot as a unit. Use
  telemetry for the cause; the console intentionally does not return exception
  details.
- Maintenance serialization is process-local. Production's single-poller
  invariant remains required.
- Removing an ID revokes access immediately, but Telegram may retain that
  user's old command menu because Bot API does not enumerate chat-specific
  scopes. Delete that scope during offboarding; the stale command remains
  authorization-gated if cleanup is missed.
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
The refund service is a narrow runtime dependency that performs bounded
server-side lookups. Support callbacks carry only a case reference; the server
derives and validates the requester-owned receipt. The 1-Star diagnostic still
selects the latest eligible test purchase server-side. The checkout's subsequent
purchase callback uses the separately scoped debug dependency plan. Keep
callbacks actor-bound and private-chat-bound.

When deleting or renaming a control, first remove it from the desired command
menu and resync every scope. Retain a fail-closed rejection for stale commands
or callback prefixes for a compatibility window, update the history exclusion
policy, then remove dependencies and presentation code. The console invokes
maintenance workers but does not own their lifecycle, so removing a button must
not remove a worker still required for background recovery.
