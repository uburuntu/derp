# Derp Product and Architecture Vision

This document is the source of truth for Derp's next product phase. It combines
the owner decisions, the intended Telegram experience, the target architecture,
and a dependency-correct delivery plan. The architecture issue register is a
risk map, not a second backlog and not a list of unanswered product questions.

The product is a fun, group-first pet project. It should be engineered
carefully, but it does not need enterprise ceremony. When experience and a
small amount of marginal provider cost conflict, prefer the better experience
and keep an explicit economic guardrail.

## Product vision

Derp should feel like the sharp friend already in the chat: easy to summon,
aware of the obvious context, capable with text and media, quiet until invited,
and honest about money and privacy.

The interface is Telegram itself. Replies, message edits, inline keyboards,
chat actions, albums, command menus, and Stars invoices should cover the whole
experience. There is no companion dashboard and no onboarding wizard.

### Experience principles

- **Continuity before ceremony.** Users should not restate context, re-upload
  media, or repeat a failed request when Derp can safely recover it.
- **Quiet machinery.** Routine model selection, quoting, charging, retries, and
  cache behavior stay invisible. Surface them only for consent, progress,
  recovery, or an explicit balance request.
- **Native Telegram interaction.** Prefer replies, edits, familiar buttons, and
  scoped commands over command syntax, explanatory walls of text, or external
  pages.
- **Consent at meaningful boundaries.** Do not confirm ordinary turns. Confirm
  expensive tools, first-time personal fallback in a group, purchases, and
  destructive privacy actions.
- **Honest progress.** Acknowledge work immediately, show real stages rather
  than invented percentages, and always reach a clear delivered, failed,
  canceled, or refunded state.
- **Visible and reversible privacy.** Context state is easy to inspect, admins
  can disable ambient capture, and each user can remove their own stored
  history without needing an administrator.
- **Spend complexity on delight, not infrastructure.** Absorb small retry,
  media-context, and delivery costs when they remove friction. Defer generalized
  platforms, workflow engines, and provider abstractions until evidence demands
  them.

## Telegram experience contract

This section is the acceptance contract for product work. Architecture exists
to make these flows reliable.

### Invocation and first use

- Private chats treat every user message as an invocation.
- In groups, Derp responds only to a mention, the configured whole-word name, a
  reply to Derp, or a command. Ambient context never means unsolicited replies.
- Derp replies to the triggering message in the same chat and forum topic.
- On joining a group, Derp sends one compact status panel with invocation,
  context, and settings controls. If join handling cannot send it, the panel is
  shown on the first observed invocation.
- Ambient history is on by default after that visible notice. Admins can turn
  it off. Existing groups receive the same one-time notice before ambient
  capture begins.
- When Telegram reports new members in an ambient-enabled group, coalesce a
  compact, rate-limited context and retention notice so disclosure is not
  limited to the members present when Derp joined.
- Telegram privacy mode or missing permissions can prevent ambient delivery to
  the bot. In that case the panel shows `Mentions only`; it must never claim
  that ambient context is active when Derp cannot receive it.
- The initial panel uses a few direct controls such as `Try Derp`,
  `Context: On`, and `Settings`. It is not a feature tour.

### Conversation continuity

- Derp receives a coherent recent conversation, not a flattened diagnostic
  dump. Roles, chronology, replies, topics, tool calls, and assistant messages
  remain distinguishable.
- Context never crosses chats. Forum history and memory are isolated by
  `(chat_id, thread_id)` and inherit only explicit chat policy.
- A later invocation in the same chat or topic may use any still-active recent
  context, regardless of which member invokes Derp. That matches what members
  can already see in the room.
- Natural follow-ups should work without requiring a reply when the referent is
  unambiguous. Ask a focused clarification only when choosing incorrectly would
  materially change the result or spend.
- Responses remain in the original reply chain even when broader ambient
  context was used to understand the request.
- Chat-user preferences apply only to that user in that chat and are never
  treated as instructions for other members.

### Media continuity

- Store a normalized Telegram snapshot and stable attachment references. Never
  persist source-media bytes, signed Telegram download URLs, or duplicated
  nested reply payloads as conversation history.
- Ambient attachments retain captions, type, dimensions or duration, Telegram
  `file_id` and `file_unique_id`, and their relationship to the source message.
  Media is downloaded only when an invocation needs it.
- Once Derp inspects media, that media stays in its originating logical turn and
  is rehydrated on demand while the turn remains inside the prompt window. It
  has no separate two-turn timer and no chat-wide `latest image` slot.
- Media leaves context when token-aware history trimming evicts the complete
  logical turn. Trimming must preserve request/response and tool-call/result
  pairs.
- Multiple recent images remain available while they fit the prompt budget.
  Under pressure, evict the oldest complete turns instead of silently replacing
  or mixing media.
- Albums remain grouped and generated output is delivered as native Telegram
  media, not a temporary external link.
- Generated artifacts are the narrow exception to the durable-byte rule. Keep
  them in a private, size-bounded artifact store with a short TTL only for
  delivery retry, then delete them after delivery, spend reversal, or expiry.
- A short-lived in-process byte cache may avoid repeated Telegram downloads
  during one operation. It is an optimization, not durable storage.
- If later hydration fails, retain the caption and attachment marker, continue
  when possible, and offer the smallest useful recovery action.

This intentionally spends slightly more input bandwidth and tokens for natural
follow-ups. For the configured Flash models, carrying one ordinary image across
the current history windows costs at most a few cents before cache discounts;
latency and bandwidth are the more important measurements.

### Spending and purchases

- Routine chat turns and inexpensive operations run without confirmation when
  an eligible balance is already available.
- Expensive tools show one exact fixed quote with `Run` and `Cancel`. The quote
  names the capability and price; provider token variance and system retries
  cannot increase it.
- A natural-language tool may need the agent to discover validated arguments
  before its exact quote exists. End that run with a deferred tool request,
  persist its tool-call ID, validated arguments, original history, requester,
  chat/topic, quote, and expiry, then bind the `Run` callback to that record.
  Approval resumes the original run with Pydantic AI deferred tool results; it
  must not rerun discovery or charge the base turn again. Denial and expiry do
  not charge the tool. The discovery turn follows its ordinary base quote; the
  tool quote includes the finishing model call after approval.
- Shared chat funds are tried first. If they cannot cover the whole operation,
  ask once with `Use mine once`, `Always here`, and `Buy for chat`. Never fall
  back to personal funds silently.
- `Always here` is per user and per chat, can be revoked from settings, and does
  not grant another member access to that user's wallet.
- Ordinary successful turns do not produce receipt spam. Purchases, explicit
  balance views, expensive operations, failures, releases, and refunds show the
  relevant balance effect.
- Failure copy explicitly says `Not charged` or `Refunded` when applicable.
- Stars invoices identify the pack and whether credits go to the buyer or the
  current chat. Payment fulfillment is idempotent and never asks a payer to buy
  again after Telegram has charged them.
- `/credits` shows monthly allowance, purchased credits, reset behavior, chat
  credits when relevant, and recent exceptional charges or refunds.
- `Manage plan` exposes cancel or re-enable renewal without leaving Telegram.

### Progress, delivery, and recovery

- Near-instant work uses Telegram chat actions only. Longer work gets one
  editable status message with honest stages such as `Preparing`,
  `Generating`, and `Delivering`.
- Do not show fake percentages. Show `Cancel` only while cancellation can still
  stop work or prevent a charge.
- Results arrive in the original chat or topic as a reply to the requester. A
  temporary progress message is edited into the result or removed after native
  media delivery.
- Provider success and Telegram delivery are separate states. Persist a result
  long enough to retry delivery; reverse captured spend automatically if
  delivery becomes permanently impossible and show that as `Refunded`.
- Invalid input, policy rejection, cancellation before billable work, provider
  failure, and unusable output are not charged.
- Provider calls, settlement, and unambiguously failed Telegram calls retry
  idempotently. A retry never creates another reservation or charge.
- Telegram send methods have no idempotency key. Record delivery intent before
  sending and prefer editing a known progress-message ID. If a native-media send
  times out after Telegram may have accepted it, do not blindly resend. Mark
  delivery uncertain and offer `Send again` at no additional charge; reverse
  captured spend if the artifact expires without confirmed delivery.
- Recoverable failures preserve the request, media references, quote, and
  operation identity, then offer a relevant action such as `Retry`,
  `Choose media`, `Buy credits`, or `Settings`.
- V1 does not add a durable workflow engine. If the process dies during paid
  provider work before a completed artifact is recorded, startup reconciliation
  releases its reservation or reverses captured spend, and the user can retry.
  A completed TTL-bound artifact may survive restart only for delivery
  reconciliation; provider generation itself never resumes. Derp absorbs any
  orphaned provider cost.

### Help, settings, and privacy

- `/help` is the entry point to one context-aware inline menu. It shows only
  relevant actions and links to creation, credits, privacy, and settings.
- `/settings` may alias the same menu. Members can inspect group state; only
  admins receive group mutation controls.
- Admin controls cover ambient context, `7/30/90` day retention, shared-memory
  permissions, shared-credit spending, expensive-tool access, clearing the
  current chat or topic history, and forgetting approved shared facts. The
  default retention is 30 days.
- Personal controls cover per-chat spending fallback, chat-user preferences,
  balances, and deletion of the user's stored messages in that chat.
- Group capture mode remains admin-controlled in v1. A member cannot disable
  capture for everyone, but can inspect the setting and delete their own stored
  messages at any time.
- `Privacy and history` -> `Delete my messages from this chat` uses a compact
  destructive confirmation. Replying `/forget` to one of the user's own
  messages is the precise shortcut.
- Deletion scrubs source content, attachment references, history DTOs,
  canonical projections, summaries, application-managed explicit caches, and
  direct stored quotes. It leaves an anonymous `[message deleted]` tombstone to
  preserve chronology and reply structure.
- Implicit provider caches and external telemetry have no per-message deletion
  handle. Production telemetry therefore stores no message or binary content;
  provider and development telemetry follow disclosed bounded retention rather
  than claiming deletion Derp cannot enforce.
- Tombstones contain no author or content and expire on the deleted message's
  original retention schedule.
- Disabling ambient context stops future ambient capture and purges prior
  ambient-only snapshots and derived artifacts. Explicit Derp interactions and
  approved shared facts remain until an admin separately chooses `Clear
  history` or `Forget shared facts` from the same menu.
- Derp cannot remove Telegram's own message history or unattributed text copied
  into another member's message. The deletion confirmation states that limit
  plainly.
- Telegram's scoped command menu is the command index. Free-form conversation
  remains the primary interface.

## Resolved product contract

Everything in this section is decided. Numeric tuning can be derived during
implementation without reopening the product design.

### Product scope

- Derp is group-first. Private chats remain fully supported.
- Pydantic AI remains the agent runtime, aiogram remains the Telegram runtime,
  and PostgreSQL remains the durable source of truth.
- Google is the only implemented model provider in v1. A single model catalog
  keeps the execution boundary replaceable without building a multi-provider
  framework.
- Derp speaks only when invoked. Ambient history improves understanding; it
  never grants permission to join conversations automatically.

### Wallets, subscriptions, and quotes

- A personal wallet has two inventories: a renewing monthly subscription
  allowance and non-expiring purchased credits.
- A chat wallet has purchased shared credits in v1. Chat subscriptions are
  deferred.
- Within one wallet, spend expiring allowance before purchased credits. One
  operation may use both inventories of that wallet when needed.
- One operation is funded by exactly one wallet owner: the chat first, otherwise
  the caller after per-chat consent. A charge never splits across chat and
  personal wallets.
- All members may spend chat credits, subject to admin controls for shared
  spending and expensive tools.
- The base agent turn and each paid tool receive independent fixed pre-run
  quotes. The quote is keyed by resolved model or capability and a context band.
- Operation release and spend reversal return a reservation or captured charge
  to its original inventories and are always non-negative ledger credits.
- A Telegram payment refund is a separate clawback: revoke unspent credits or
  allowance from that purchase or cycle. If some were already consumed, record
  explicit credit debt and block further paid use until reconciled; never make
  a spendable inventory negative.
- Fully consuming any subscription allowance must cost no more than net payment
  revenue after Telegram and Stars fees. The owner's `$25/month` risk tolerance
  is a global project loss ceiling, not a per-subscriber subsidy.
- Free or subsidized inference is upside only and never part of the price
  assumption.
- Initial tier prices and allowance sizes are an engineering calibration task:
  correct the model catalog, measure real usage bands, apply the margin and
  global loss guardrail, then launch the smallest understandable set of one
  personal plan and a few top-up packs.
- The personal plan is a recurring 30-day Telegram Stars subscription. Each
  successful payment creates one idempotent, versioned allowance cycle anchored
  to Telegram's subscription expiration time in UTC. Allowance does not roll
  over.
- One user may have at most one active Derp plan. Invoice creation and renewal
  handling reject or reconcile duplicate active subscriptions rather than
  stacking allowances.
- Canceling or failing renewal prevents the next cycle but leaves the current
  cycle active until expiry. V1 has one plan, no upgrades, no proration, and no
  grace period. Purchased credits are unaffected by subscription cancellation.
- Refunding a subscription payment applies the payment-clawback rule to that
  cycle. `/credits` shows current-cycle allowance, expiration or renewal state,
  purchased credits, and any debt.

### Authorization and memory

- Authorization is deterministic. Telegram role defines the maximum
  entitlement; the effective toolset is the intersection of that role, typed
  chat policy, and enabled product features. Wallet sufficiency is checked at
  execution time rather than encoded as prompt authority.
- Admin-only or disabled tools are absent from the run. Prompt or memory text
  can never grant a capability.
- Roles may be cached briefly but are revalidated for privileged mutations.
- Platform instructions and explicit admin chat policy are trusted
  instructions. History, summaries, preferences, factual memory, and media are
  always untrusted data.
- Chat policy is structured settings plus one bounded admin-authored paragraph,
  stored separately from factual memory.
- Members propose shared facts and admins approve them by default. A chat
  setting may allow members to edit shared facts directly, but never policy or
  another user's preferences.
- No third-party personal facts and no cross-chat personalization in v1.

### History data model

- Persist three related forms with explicit schema versions:
  1. a normalized Telegram source-event snapshot for audit and reprocessing;
  2. an application history DTO mirroring Pydantic AI request, response, and
     tool-pair semantics while carrying application media references;
  3. a canonical role/text/tool projection for indexing and migration.
- Materialize the history DTO into native Pydantic AI messages at the execution
  edge. Durable media references become ephemeral `BinaryContent` only after
  hydration; custom application references are not native Pydantic AI parts.
- The normalized snapshot allowlists conversational fields: text, caption,
  entities, sender identity, timestamps, edit state, topic and reply IDs,
  content-specific metadata, and attachment references.
- Payment, passport, authentication, contact, location, and web-app payload
  bodies are not copied into conversational history.
- The current inbound event is excluded from prior history by construction and
  appended exactly once as the current request.
- History processing is token-aware and removes complete logical turns while
  preserving tool-call/result pairs. Paid windows should be generous; free
  windows may be smaller but still preserve a coherent exchange.
- Raw snapshots and attachment references follow the admin-selected retention
  period. Prompt windows are independent, tier-aware views over that data.
- Message edits replace the stored snapshot and invalidate derived artifacts;
  old revisions are not retained.

### Prompt assembly and caching

- Build deterministic prefixes in this order: platform instructions, role
  appropriate tool schemas, admin policy, untrusted memory snapshot, and prior
  materialized history. Add chat-user preference, current message, and current
  media at the end.
- Canonicalize encoding, whitespace, stable identifiers, JSON key ordering, and
  item ordering. Cover the renderer with golden tests tied to the Pydantic AI
  and provider-adapter versions.
- Record total, cached, and uncached input tokens plus provider cost per run.
- V1 does not persist context epochs and does not pre-create provider caches.
  Let deterministic prefixes benefit from implicit caching and optimize only
  after telemetry proves a worthwhile hot scope.

### Operation and transaction model

- A small set of domain values drives every paid path: `ExecutionPlan`,
  `Quote`, `OperationId`, typed `Outcome`, `DeliveryState`, and a pending
  deferred-tool record.
- The operation lifecycle is quote -> reserve -> provider execution -> capture
  or release -> delivery -> retry or spend reversal. Payment refunds use a
  distinct purchase-clawback lifecycle.
- Database transactions cover only state changes. Provider work and Telegram
  delivery never run inside a database transaction.
- Command handlers and agent tools are adapters over the same feature service.
  They cannot choose different models, prices, idempotency keys, or settlement
  behavior.
- Provider executors return typed outcomes such as `Succeeded`, `Rejected`, and
  `Failed`. Settlement never infers policy from human-readable strings.
- Every billable side effect has a unique operation ID. Agent tool invocations
  incorporate `ctx.tool_call_id`; command routes generate an equivalent ID.
- Deferred-tool callbacks authenticate the actor, load immutable validated
  arguments and quote state server-side, expire closed, and resume the stored
  Pydantic AI history with matching `DeferredToolResults`.
- Logs and spans use operation IDs and settlement state, exclude message and
  binary content in production, and log failures once at the owning boundary.

## Target system shape

Keep the design small and explicit:

```text
Telegram update
  -> thin handler / tool adapter
  -> application service
       -> model catalog + quote service
       -> wallet + operation service
       -> context/history service
       -> feature executor
  -> delivery service
       -> Telegram API
```

The boundaries are earned by current duplication and correctness risks:

- **Model catalog:** one source for provider model ID, capabilities, context
  limits, lifecycle, and current provider pricing.
- **Quote and wallet service:** fixed quotes, balance selection, reservations,
  capture, release, spend reversal, purchase clawback, purchases, and debt.
- **Context/history service:** scope, normalized snapshots, native history,
  memory authority, token-aware trimming, media-reference hydration, and
  deletion propagation.
- **Feature services:** one each for image/editing, thinking, video, and TTS;
  no Telegram sending and no billing policy inside provider executors.
- **Media gateway:** shared HTTP client, streaming limits, MIME validation,
  sensitive URL redaction, temporary bytes, and Telegram file hydration.
- **Delivery service:** reply targeting, text splitting, albums, editable
  progress, retries, terminal delivery state, and spend-reversal handoff.

Do not create a generic service framework. Plain typed functions and small
classes are sufficient until repetition proves otherwise.

## Delivery plan

Milestones are vertical outcomes, not layers to perfect indefinitely. Each
milestone must leave the bot releasable and remove the obsolete path it
replaces.

### Milestone 0: Contain and characterize

Goal: stop known unsafe behavior from expanding and establish trustworthy
baselines.

- Disable `/buy` and `/buy_chat` until durable purchase intents and end-to-end
  Stars validation ship in Milestone 2. Do not advertise disabled or unfinished
  capabilities in `/help`.
- Add characterization tests for current-message duplication, forum-topic
  leakage, model/price drift, repeated same-message tool calls, payment payload
  validation, and Telegram sender constraints.
- Test PostgreSQL from Alembic migrations only; stop using ORM metadata creation
  to conceal schema drift.
- Create the single Google model catalog before building quotes. Replace the
  stale Flash Lite and Flash prices and remove the duplicate runtime tier map.
- Introduce the minimal `ExecutionPlan` and typed outcome vocabulary without a
  broad framework.

Exit criteria:

- Current critical behavior has regression coverage.
- Billing and runtime resolve the same concrete model and current price.
- No provider request or Telegram send is needed to construct a quote or typed
  outcome.
- `/buy` and `/buy_chat` cannot create an invoice before Milestone 2's payment
  exit criteria pass.

### Milestone 1: Conversation that feels continuous

Goal: make everyday chat excellent before adding more premium surface area.

- Store normalized source events with explicit role, direction, topic, reply,
  edit, and attachment fields.
- Exclude the current event from prior context and isolate every forum topic.
- Materialize native Pydantic AI messages from the application history DTO and
  apply token-aware complete-turn trimming.
- Ship ambient-by-default group onboarding, honest context state, and the
  unified help/settings panel, including rate-limited new-member disclosure.
- Add media references, on-demand hydration, natural in-window media
  continuity, graceful degradation, and the minimum safe media gateway: shared
  HTTP client, size and time limits, MIME validation, and sensitive URL
  redaction.
- Render history and factual memory as untrusted data. Build deterministic
  role-and-policy-derived toolsets and the admin approval path for shared facts.
- Ship per-user deletion, anonymous tombstones, admin retention controls, and
  ambient opt-out cleanup. Add separate admin actions for clearing chat/topic
  history and forgetting approved shared facts.

Exit criteria:

- Private, group mention/reply, ambient follow-up, and forum-topic journeys pass
  automated tests and manual Telegram smoke tests.
- A user can refer naturally to a recent inspected image without re-uploading
  it.
- No context, media, memory, or preference crosses a chat or forum topic.
- Context state and deletion are discoverable without memorizing commands.
- Admins can clear history and shared facts independently without affecting the
  other store.

### Milestone 2: Paid operations people can trust

Goal: make charging, purchases, and failures boringly predictable.

- Implement personal allowance and purchased inventories plus purchased chat
  credits.
- Implement recurring 30-day cycle creation, renewal, cancellation, expiry,
  non-rollover, and subscription payment clawback.
- Add immutable quotes and atomic reserve/capture/release/spend-reversal using
  unique operation IDs and short units of work.
- Implement chat-first wallet selection and one-time personal fallback consent.
- Add opaque durable purchase intents and strict payer, target, currency,
  amount, expiry, pack-version, and charge-ID validation.
- Migrate one valuable vertical slice, image generation/editing, through the
  complete quote -> operation -> outcome -> delivery path before generalizing.
- Persist and resume expensive natural-language tool approvals with validated
  arguments, exact quotes, authenticated callbacks, expiry, original history,
  and Pydantic AI deferred tool results.
- Add `/credits`, expensive-operation confirmation, purchase targeting, and
  clear not-charged/refunded terminal copy.

Exit criteria:

- Concurrent requests cannot overspend a wallet or quota.
- Duplicate tool, callback, pre-checkout, payment, capture, release, spend
  reversal, and purchase-clawback events are harmless.
- A real debug Stars purchase fulfills once to the intended target.
- Command and natural-language image paths produce the same plan, charge, and
  outcome.
- An ambiguous Telegram media-send timeout never triggers a blind duplicate;
  the existing operation can resend without another charge or expire into a
  spend reversal.

### Milestone 3: One polished premium experience

Goal: give every expensive feature the same low-friction interaction quality.

- Extract feature services for image/editing, thinking, video, and TTS one at a
  time. Delete each duplicated command/tool implementation as it migrates.
- Add bounded media transport, provider timeouts, typed rejection/failure
  behavior, and provider-independent results.
- Add editable progress, meaningful cancellation, native Telegram delivery,
  retry actions, a private size-bounded artifact store with TTL cleanup, and
  automatic delivery spend reversals.
- Do not migrate all features simultaneously. Finish and validate one before
  exposing the next.

Exit criteria:

- Exposed premium features share the same consent, progress, delivery, and
  user-facing refund contract backed by explicit spend reversal.
- Provider executors cannot send Telegram messages or mutate balances.
- The bot never leaves an accepted paid operation in an ambiguous visible
  state.

### Milestone 4: Efficiency and pragmatic hardening

Goal: reduce cost and operational risk using evidence from the finished flows.

- Measure prompt bands, image hydration, cached/uncached tokens, latency, and
  provider cost. Tune generous windows without making follow-ups brittle.
- Add explicit provider caches only when observed hot scopes beat their storage
  and invalidation cost.
- Remove handler-wide database sessions, scope dependency loading to matched
  routes, and tune bounded concurrency against the database and provider.
- Add privacy regression tests for logs and traces, concurrent database tests,
  migration parity, Docker configuration, and build validation.
- Keep deployment simple: immutable images, migration status checks, a verified
  backup before destructive changes, readiness, and a documented rollback
  constraint. Do not build blue/green orchestration for a pet project.

Exit criteria:

- Costs and cache behavior are observable by model, context band, capability,
  and outcome.
- Production telemetry contains neither message text nor binary media.
- Startup reconciliation closes incomplete reservations after a crash.
- The documented release path is repeatable without bespoke infrastructure.

## Architecture issue register

The register preserves why the milestones exist. Resolved product semantics
above take precedence over older code or copy.

### AR-001: Pricing and credit behavior diverge

Current code unlocks a model from a positive balance without consistently
charging turns, and displayed tool costs can differ from registry-derived
costs. Milestones 0 and 2 replace this with one model catalog, immutable quotes,
and the resolved wallet contract.

### AR-002: Charging is not a transaction protocol

Checks happen before provider work and deductions happen afterward, so
concurrent requests can overspend and retries can double-charge. Milestone 2
implements reserve/capture/release/spend-reversal with unique operation IDs.

### AR-003: Tool strings cannot express settlement policy

Success, refusal, missing input, and infrastructure failure currently share a
string return channel. Milestones 0 and 3 introduce typed domain outcomes and
translate them only at Telegram and model boundaries.

### AR-004: Feature execution is duplicated

Command and agent-tool paths choose models, charge, execute, send, and recover
differently. Milestones 2 and 3 move each capability behind one feature service
and delete the replaced paths incrementally.

### AR-005: Model selection had two sources of truth (resolved)

`derp/catalog/google.py` now owns immutable model IDs, lifecycle, capabilities,
limits, source links, and current provider pricing. Billing checks and runtime
execution carry the same exact model spec; feature policy stores only semantic
keys. Milestone 2 will consume this catalog when immutable quotes replace the
temporary fixed credit estimates.

### AR-006: Payment fulfillment lacks a durable intent

Pre-checkout does not fully bind payer, target, amount, currency, and immutable
credits. Fulfilled purchases do have a `CreditTransaction` keyed by Telegram
charge ID, but there is no durable intent or charged-but-unfulfilled recovery
path. Milestone 2 preserves that idempotency record while adding opaque
expiring intents and reconciliation.

### AR-007: Database sessions cross external effects

Handler and tool sessions can stay open through provider calls and Telegram
sends. Milestones 2 and 4 replace them with short query/command units that
return plain domain values across effect boundaries.

### AR-008: Conversation history has no canonical role model

Current context can duplicate the inbound message, lose assistant identity,
flatten history into text, and leak across forum topics. Milestone 1 implements
the resolved source/native/canonical model and complete-turn processing.

### AR-009: Shared memory is a privileged prompt channel

Any member can currently write memory that is injected with instruction-level
authority. Milestone 1 separates admin policy from untrusted facts, derives
tools from role plus typed policy and feature state, and gives fact proposals
an explicit approval path.

### AR-010: Dependency injection is redundant and broad

Every update can pay for model and credit context it does not use, including
latency-sensitive payments. Milestone 4 injects static services through aiogram
workflow data and loads domain context at the narrowest route.

### AR-011: Runtime and long-operation behavior are unclear

Concurrency is globally bounded but not tuned, and long operations lack clear
timeouts and crash outcomes. Milestones 3 and 4 add timeouts, cancellation,
idempotency, and crash spend reversals without a durable workflow engine in v1.

### AR-012: Media transport and delivery are coupled

Downloads buffer files with ad hoc clients, sensitive URLs can reach tracing,
and tools mix generation with Telegram sending. Milestones 1 and 3 establish
media-reference hydration, bounded transport, and a delivery boundary.

### AR-013: Schema, models, and tests can disagree

ORM metadata creation can hide migration drift, and credit concurrency lacks
real PostgreSQL coverage. Milestones 0, 2, and 4 make Alembic authoritative and
add parity, idempotency, and concurrency tests.

### AR-014: Deployment hardening exceeds present needs

The current release path can stop the old bot before migration success, but a
full transactional release platform is disproportionate. Milestone 4 adopts
backups, migration checks, readiness, immutable images, and explicit rollback
limits only.

### AR-015: Observability can violate privacy

HTTP traces, tool arguments, update payloads, and duplicated exception logging
can expose content or sensitive Telegram URLs. Every milestone keeps content
out of production telemetry and limits deletion promises to controlled stores;
Milestone 4 adds regression tests and verifies Pydantic AI v5 usage attributes.

### AR-016: Prompt assembly defeats caching

Current code rebuilds chat metadata, sliding history, memory, and the current
message as one changing string. Milestones 1 and 4 build deterministic native
history and stable prefixes, measure real cache hits, and deliberately avoid
persisted epochs until the data justifies them.

## Deferred by design

- Chat subscriptions. V1 supports personal subscriptions and purchased chat
  credits.
- Cross-chat memory, global user profiles, and third-party personal facts.
- Automatic unsolicited participation in group conversations.
- Multi-provider execution beyond a replaceable Google boundary.
- Persisted context epochs and explicit provider caches without measured need.
- A durable workflow engine and crash-resuming provider jobs.
- Provider choice based on shared-prompt or free-inference terms without a
  separate product and privacy review.
- Gifting, remote chat purchases, a companion dashboard, and a large command
  surface.
- Blue/green deployment, generalized media infrastructure, and load-testing
  programs beyond the observed scale.

## Framework constraints

### Pydantic AI

- Keep `AgentDeps` run-scoped and typed; never store Telegram or database state
  on shared agents.
- Use `instructions` for run-specific guidance. Persist a `system_prompt` only
  when its presence in native history is intentional.
- Group tools with `FunctionToolset`; derive the provided toolset from role and
  never register a second unmetered path directly on the agent.
- Use `ModelRetry` only for arguments the model can correct. Provider,
  infrastructure, and programming failures reach the application boundary.
- Bound model requests, tool calls, input and output tokens, and external-tool
  duration independently.
- Use `capabilities=[ProcessHistory(...)]` for token-aware logical-turn
  processing. Preserve tool-call/result pairs and never slice raw model
  messages naively.
- Persist a versioned application history DTO and materialize it into native
  messages for each run. Exact `BinaryContent` JSON base64-embeds bytes and is
  not the durable format.
- Use `DeferredToolRequests` and `DeferredToolResults` with original message
  history for approval that must leave and later resume through Telegram.
- Use `ctx.tool_call_id` as part of agent operation identity.
- Use `TestModel`, `FunctionModel`, `Agent.override`, and
  `ALLOW_MODEL_REQUESTS=False` in tests.
- Keep instrumentation v5 explicit and exclude binary and message content from
  production telemetry.

### Logfire and OpenTelemetry

- `derp/observability.py` owns configuration, scrubbing, integrations, logging,
  and synchronous flush/shutdown. Application imports must not configure global
  telemetry as a side effect.
- Trace every inbound update with one constant-name `telegram.update` consumer
  span. Attach only numeric Telegram identifiers, update type, and handled
  outcome; content and display names are forbidden.
- Keep direct Google GenAI content capture and completion hooks disabled. A
  local-development opt-in may capture Pydantic AI text only; production and
  binary capture remain disabled.
- Redact exception messages, source text, and status descriptions through the
  global Logfire exception callback, including auto-instrumented provider
  spans. Recovering boundaries use the same privacy-safe reporting helper.
- Never globally instrument HTTPX because Telegram file URLs contain the bot
  token. Do not place Telegram identifiers in baggage propagated to external
  services.
- Use Logfire's configured tracer and meter providers for integrations, basic
  system metrics, failure-only Pydantic validation, and explicit shutdown after
  the application boundary has logged a fatal error.

### Aiogram

- Use typed `CallbackData` and typed `MiddlewareData`.
- Inject static services through dispatcher workflow data and load expensive
  domain state only for matched routes.
- Attach flags to registered handler objects; decorating a class handler's
  `handle()` method does not attach a router flag to the class.
- Keep session retry/fallback outside delivery persistence so only successful
  Telegram actions are stored.
- Answer pre-checkout within Telegram's deadline without unrelated middleware
  work.
- Keep polling concurrency finite and close bot and database resources even
  when startup fails.

## Upstream reference baseline

Gitignored source checkouts matching the dependency lock are available locally:

- `references/pydantic-ai` at Pydantic AI v2.13.0
- `references/aiogram` at aiogram v3.30.0
- `references/logfire` at Logfire v4.38.0

Recreate them after a fresh clone:

```bash
git clone --depth 1 --branch v2.13.0 https://github.com/pydantic/pydantic-ai.git references/pydantic-ai
git clone --depth 1 --branch v3.30.0 https://github.com/aiogram/aiogram.git references/aiogram
git clone --depth 1 --branch v4.38.0 https://github.com/pydantic/logfire.git references/logfire
```

Update the checkouts and commands when the lock changes. Consult the matching
sources before changing routers, middleware, payments, session behavior,
agents, capabilities, tools, native history, retries, or instrumentation.

## Decision policy

There are no remaining owner questions blocking implementation. The agent
should choose reversible numeric defaults, derive prices from current provider
costs, validate them with telemetry, and record material changes here. Escalate
only a genuinely irreversible product choice, legal or provider-terms change,
new external spend commitment, or privacy behavior that contradicts this
contract.
