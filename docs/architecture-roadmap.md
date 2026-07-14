# Architecture Roadmap

This document is the handoff from repository cleanup to product development. It
records work that is too behavioral, cross-cutting, or migration-sensitive for
the chore tranche.

## Product invariants

- Pydantic AI remains the agent runtime.
- Aiogram remains the Telegram runtime.
- PostgreSQL remains the durable source of truth.
- Handlers should be thin transport adapters; domain operations own policy.
- Provider work and Telegram delivery must not occur inside database
  transactions.
- Charging, quota use, and payment fulfillment must be explicit, atomic, and
  auditable.
- Shared-chat features must define their authorization and topic-isolation
  semantics.

## Upstream reference baseline

Gitignored source checkouts matching the dependency lock are available locally:

- `references/pydantic-ai` — Pydantic AI v2.9.1
- `references/aiogram` — aiogram v3.29.1

Recreate the ignored checkouts after a fresh clone:

```bash
git clone --depth 1 --branch v2.9.1 https://github.com/pydantic/pydantic-ai.git references/pydantic-ai
git clone --depth 1 --branch v3.29.1 https://github.com/aiogram/aiogram.git references/aiogram
```

Update these commands and the checkouts whenever the locked versions change.

Read these before changing the corresponding boundary:

- Pydantic AI: `docs/changelog.md`, `docs/version-policy.md`, `docs/agent.md`,
  `docs/capabilities.md`, `docs/dependencies.md`, `docs/tools.md`,
  `docs/tools-advanced.md`, `docs/toolsets.md`, `docs/output.md`,
  `docs/message-history.md`, `docs/retries.md`, `docs/testing.md`,
  `docs/logfire.md`, and `docs/durable_execution/overview.md`.
- Aiogram: `docs/dispatcher/dependency_injection.rst`,
  `docs/dispatcher/middlewares.rst`, `docs/dispatcher/router.rst`,
  `docs/dispatcher/long_polling.rst`, `docs/dispatcher/flags.rst`,
  `docs/dispatcher/filters/callback_data.rst`,
  `docs/utils/chat_action.rst`, and `docs/api/session/middleware.rst`.

## Framework rules for new work

### Pydantic AI

- Keep dependencies run-scoped and typed with `AgentDeps`; do not store
  Telegram or database state on shared agents.
- Use `object`, not `None`, as the dependency type for agents that have no
  runtime dependencies.
- Prefer `instructions` for run-specific guidance. Use `system_prompt` only
  when its persistence through native Pydantic AI message history is intended.
- Treat Pydantic AI v2 `Capability` as the primary composable unit for tools,
  instructions, settings, and lifecycle hooks. Use `FunctionToolset` to group
  function tools; `.tool()` requires `RunContext`, while `.tool_plain()` is for
  context-free functions.
- Do not register a second, unmetered implementation directly on the agent.
- Use `ModelRetry` only when the model can correct its arguments. Let provider,
  infrastructure, and programming failures reach an application boundary.
- Bound tool calls, model requests, input tokens, output tokens, and long tool
  execution independently.
- Choose `end_strategy` explicitly when output tools coexist with side-effecting
  function tools. A tool's `sequential=True` is a barrier, not full-run
  serialization.
- Use `ctx.tool_call_id` when an operation needs per-invocation identity.
- Use `TestModel` or `FunctionModel`, `Agent.override`, and
  `ALLOW_MODEL_REQUESTS=False` in tests.
- If native message history is adopted, persist Pydantic AI message JSON and
  preserve request/response and tool-call/tool-result pairs when trimming it;
  use `capabilities=[ProcessHistory(...)]` for history processing.
- Configure instrumentation v5 explicitly, exclude binary content, choose
  prompt/completion capture by environment, and expect run-level usage under
  `gen_ai.aggregated_usage.*`.
- Apply durable execution only to workflows that need recovery across process
  loss, such as long-running video generation. Retries must not repeat billable
  or user-visible side effects.
- Minor releases may add message parts or change telemetry attributes; consume
  both defensively.

### Aiogram

- Use typed `CallbackData` for callback payloads instead of hand-splitting
  colon-delimited strings.
- Treat update-inner middleware as running for every update; scope expensive
  model and credit loading closer to matched handlers.
- Use aiogram's workflow data and typed `MiddlewareData` for dependency
  injection instead of rediscovering the event context.
- Set a finite `tasks_concurrency_limit` for polling. Decide separately whether
  work should be serialized per user, chat, or topic.
- Attach flags to the registered handler object. For class-based handlers,
  decorating `handle()` does not attach a router flag to the class.
- Keep request-session middleware ordered as retry/fallback outside
  persistence, so only successful Telegram actions are stored.
- Validate and answer pre-checkout queries within Telegram's ten-second
  deadline; skip unrelated middleware work on this path.
- Use a lifecycle boundary that closes the bot session and database even when
  setup fails before polling starts.

## Architecture issue register

### AR-001 — Credit semantics are undefined

Evidence:

- User-facing copy says one credit buys one better-model message.
- A positive balance currently unlocks the standard model, but ordinary chat
  turns do not deduct model credits.
- Tool prices shown to users do not match effective registry-derived prices.

Required decision:

- Choose one contract: per-operation currency, subscription-like unlock, or a
  hybrid with explicit entitlements.

Target:

- A single pricing service returns an immutable execution quote containing the
  exact capability, model, units, price, and balance source.
- UI, access checks, ledger entries, and observability all consume that quote.

### AR-002 — Charging is not a transaction protocol

Evidence:

- Access is checked before provider work and deducted afterward.
- Concurrent calls can all pass a balance or free-quota check.
- Some tools send output before deduction; command handlers may deduct before
  delivery.
- The current idempotency key merges distinct calls to the same tool in one
  Telegram message.

Target:

1. Atomically reserve credits or quota using a unique operation ID.
2. Execute provider work outside the transaction.
3. Capture the reservation after successful provider completion.
4. Record delivery independently.
5. Release or refund reservations on classified failures.

Use `ctx.tool_call_id` for agent tool invocations and a generated operation ID
for command handlers. Temporarily serialize side-effecting tools until this
protocol exists.

### AR-003 — Tool outcomes cannot express policy

Evidence:

- Tool functions return strings for success, refusal, missing input, and
  infrastructure failure.
- The wrapper interprets every normal return as billable success.
- Broad exception conversion hides programmer failures and can expose internal
  exception text to the model.

Target:

- Provider executors return typed domain outcomes such as `Succeeded`,
  `Rejected`, and `Failed`.
- Command and agent adapters translate those outcomes to Telegram or model
  text.
- Settlement uses the outcome type, not string inspection.
- Retryable argument errors use `ModelRetry`; infrastructure failures propagate
  to the application boundary.

### AR-004 — Feature execution is duplicated

Evidence:

- Image, edit, think, video, and TTS command paths duplicate parts of their
  agent-tool paths.
- Tool names, model selection, quota keys, idempotency, sending, and failure
  behavior have drifted.

Target:

- One domain service per feature owns input validation, model resolution,
  provider execution, and typed results.
- Slash commands and Pydantic AI tools are thin adapters over the same service.
- Telegram delivery is an adapter, not part of the provider executor.

### AR-005 — Model and provider selection has two sources of truth

Evidence:

- `derp/llm/providers.py` and `derp/credits/models.py` define separate tier and
  model mappings.
- Credit checks can price one model while runtime code executes another.
- Configuration previously advertised providers that runtime code never used.

Target:

- A single model catalog owns provider, model ID, capabilities, context limits,
  and pricing.
- Resolution produces an execution plan consumed unchanged by billing and the
  provider adapter.
- Provider support is either genuinely pluggable and tested or explicitly
  Google-only.

### AR-006 — Payment fulfillment lacks a durable purchase intent

Evidence:

- Chat purchase callback parsing is broken.
- Pre-checkout validates only a pack identifier.
- Currency, Stars amount, payer, target type, and target ID are not fully
  validated.
- Successful payment ignores part of the payload and uses current pack values.
- There is no durable record to reconcile failed fulfillment.

Target:

- Create an opaque, expiring purchase intent before the invoice.
- Persist payer policy, target, currency, Stars amount, immutable credit amount,
  pack version, and status.
- Pre-checkout performs a fast indexed validation and fails closed.
- Successful payment atomically fulfills the intent using Telegram's charge ID.
- Duplicate delivery, refund messages, and manual reconciliation are supported.

### AR-007 — Database sessions cross external effects

Evidence:

- Credit middleware keeps a transactional session open around complete
  handlers.
- Tool wrappers keep sessions open during provider calls and Telegram sends.
- A commit can fail after output or a payment confirmation was delivered.

Target:

- Replace handler-wide sessions with short query/command units of work.
- Return plain domain values across session boundaries, not detached ORM
  objects.
- Make commit points explicit before emitting confirmations.
- Configure pool limits from deployment settings and load-test under the
  polling concurrency limit.

### AR-008 — Conversation history has no canonical role model

Evidence:

- The current inbound message is persisted before context construction and can
  be included again as the current message.
- Outbound records can lose assistant identity.
- History is serialized as text rather than native Pydantic AI messages.
- Forum topics can share chat-wide history and memory.

Required decisions:

- Is history chat-wide or topic-scoped?
- Is native Pydantic AI history required for tool-call continuity?
- What retention and token budget apply to each paid tier?

Target:

- Store explicit role, direction, topic, chronology, and provider message data.
- Exclude the current event from prior history by construction.
- Apply token-aware `ProcessHistory` handlers that preserve tool-call pairs.

### AR-009 — Shared memory is an unguarded privileged channel

Evidence:

- Any group member can write persistent chat memory.
- Memory is injected with system-level authority.
- Command and agent-tool paths apply different limits and policy.

Target:

- Define owner/admin/member permissions and topic scope.
- Store memory as structured facts with author, provenance, and revision.
- Render it as untrusted context unless an administrator explicitly creates a
  policy.
- Share one service between commands and tools.

### AR-010 — Telegram dependency injection is redundant and over-broad

Evidence:

- Custom event-context middleware repeats aiogram's built-in context work.
- Database models and a credit session are loaded for updates that do not need
  them, including latency-sensitive payment updates.
- Several injected aliases have no consumers.

Target:

- Inject static services through dispatcher workflow data.
- Define typed middleware data.
- Load domain context at the narrowest router or handler scope.
- Keep pre-checkout validation fast and independent from LLM-related context.

### AR-011 — Runtime serialization and long jobs are undefined

Evidence:

- Polling now has a global concurrency limit, but it has not been load-tested or
  tuned against database and provider capacity.
- User throttling exists but is not part of the runtime.
- Video polling has no deadline and cannot recover after process loss.
- Synchronous or CPU-heavy work has historically leaked onto the event loop.

Target:

- Validate and tune the global concurrency bound.
- Define serialization keys for user, chat, and topic work.
- Give every external operation a timeout and cancellation policy.
- Move recoverable long jobs to a durable workflow or persisted job model.
- Ensure every billable side effect is idempotent under retry.

### AR-012 — Media transport and delivery need a boundary

Evidence:

- Downloads buffer entire files, create clients ad hoc, and lack size limits.
- Token-bearing Telegram file URLs may be visible to HTTP instrumentation.
- Sender composition can form invalid one-item media groups.
- Generation and delivery are coupled inside tools.

Target:

- A media gateway owns streaming, limits, MIME validation, redaction, client
  reuse, and temporary storage.
- A delivery service handles Telegram constraints, caption splitting, albums,
  retries, and persistence.
- Provider results remain independent from Telegram sending.

### AR-013 — Schema, models, and tests can disagree

Evidence:

- ORM credit constraints are absent from the migration chain.
- Database tests create ORM metadata after migrations, which can hide drift.
- Credit, refund, idempotency, and concurrency paths lack real database tests.

Target:

- Test a database created only by Alembic.
- Add schema-drift checks in CI.
- Reconcile existing production data before adding non-negative constraints.
- Define whether refunds can create debt before enforcing a balance invariant.
- Add concurrent integration tests for reservations and duplicate payments.

### AR-014 — Deployment is not a transactional release process

Evidence:

- Migration failure can leave the previous bot stopped.
- Rollback may run an old binary against a forward-migrated schema.
- Readiness is inferred from container state.
- Backup and restore tooling is not continuously verified.

Target:

- Use immutable image references.
- Back up and verify restore before risky migrations.
- Adopt expand/contract migrations.
- Keep the prior instance available until migration and readiness gates pass.
- Add an application readiness signal and tested rollback compatibility.

### AR-015 — Observability and privacy policy are mixed

Evidence:

- Pydantic AI instrumentation is explicitly v5, excludes binary content, and
  disables message content in production, but this policy lacks a regression
  test.
- Development HTTP tracing may observe token-bearing file URLs.
- Some logs include tool arguments or complete update payloads.
- Error logging responsibilities are duplicated at several boundaries.
- Run-level usage moved to `gen_ai.aggregated_usage.*`; dashboards have not been
  checked for that schema.

Target:

- Define field-level redaction and environment-specific content capture.
- Test that production spans contain neither message nor binary content.
- Log once at the owning boundary with operation IDs and settlement status.
- Keep provider auto-instrumentation; add spans only around domain operations
  and external effects not already covered.

## Recommended development order

1. Write characterization tests for payments, model resolution, tool outcomes,
   prompt history, and sender constraints.
2. Decide credit semantics and refund debt policy.
3. Introduce operation IDs and reserve/capture/release settlement.
4. Build shared domain services and remove command/tool duplication.
5. Unify the model catalog and execution plan.
6. Add durable purchase intents and strict Stars validation.
7. Shorten database units of work and tune bounded concurrency.
8. Redesign conversation history and shared memory.
9. Extract media and delivery gateways.
10. Enforce migration parity and harden deployment.

## Decisions required before implementation

- Does one credit buy one standard chat turn, or unlock the tier?
- May a charge draw from both chat and user balances, or exactly one pool?
- Can a refund create debt after purchased credits were spent?
- Are forum history and memory isolated by topic?
- Who may write shared memory, and with what prompt authority?
- Can users buy for a chat they are not currently in or gift another user?
- Which long operations must survive deploys and process crashes?
- Is multi-provider execution a real near-term requirement?
- What content may be retained in Logfire in development and production?
