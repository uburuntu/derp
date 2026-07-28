# Derp v0.1.0 release checklist

This is the decision-complete release record for the first production launch.
Check items only against observed evidence. Production activation requires all
external checks below even when the repository suite is green.

## Shipped release contract

- OpenRouter is the primary text and image inference plane. Paid and group
  routes require data-collection denial and ZDR. Zero-cost models require
  versioned consent, are non-ZDR, run only in private/inline contexts, and have
  no daily quota; per-request and accounting limits remain enforced.
- Google provides TTS. Deep thinking, video
  generation, and standalone transcription are hidden and fail closed.
- Provider usage records include content-free token categories, downstream
  model/provider facts, actual cost when available, and bounded reconciliation.
- Public Stars intake defaults off. Purchase intents, settlement, subscription
  cycles and renewal commands, refunds, clawbacks, debt, approvals, and paid
  delivery are durable and idempotent.
- Production Logfire and OpenTelemetry export no prompt, message, media, tool
  argument, callback, payment payload, exception text, or secret content.

## Launch catalog and economics

Catalog version: `2026-07-28-v1`.

| Product | Stars | Credits | Planning-floor margin |
| --- | ---: | ---: | ---: |
| Starter top-up | 50 | 600 | 16.0% |
| Standard top-up | 250 | 3,200 | 10.4% |
| Bulk top-up | 750 | 9,900 | 7.6% |
| Derp Personal, recurring 30 days | 500 | 6,750 | 5.5% |

The operator-only debug product is 1 Star for 10 credits and is not included in
public catalogs. Existing invoices always resolve their persisted product
version; repricing requires a new immutable version.

The July 28 assumptions are Telegram's official `$0.013/Star` reward, a 15%
private-chat Topics fee scenario, a conservative `$0.01/Star` planning floor,
and `$0.0007` maximum provider liability per credit. Private-chat Topics remain
disabled for launch. Sources:

- [Telegram Stars rewards](https://telegram.org/tos/bot-developers#6-2-4-rewards-for-stars)
- [Private-chat Topics fee](https://telegram.org/tos/bot-developers#6-2-6-enabling-topics-in-private-chats)
- [Telegram Stars payments](https://core.telegram.org/bots/payments-stars)
- [Reviewed OpenRouter evidence](inference/openrouter-references.md)

Re-evaluate the catalog after seven days or 100 paid operations, whichever
comes first. Compare actual reconciled provider cost and token bands with the
planning assumptions before issuing another product version.

## Before deployment

- [ ] PR checks, full quick and PostgreSQL suites, Ruff, i18n generation,
  Docker build, migration drift, and production-shaped upgrade pass.
- [ ] Resolve or explicitly document dependency, CodeQL, image-vulnerability,
  secret-scanning, and push-protection findings.
- [ ] Protect `main`; require an owner review on the GitHub `production`
  environment.
- [ ] Verify database and backup encryption at rest, private database networking,
  host disk capacity, firewall policy, and artifact directory mode `0700`.
- [ ] Keep the production PostgreSQL major version unchanged for this release.
- [ ] Populate `.env.prod` with operator IDs, OpenRouter metadata/key, Google TTS
  key, callback signing secret, content-free Logfire, the enforced feature list,
  and `PUBLIC_PURCHASES_ENABLED=false`. Configure the reviewed SSH host
  fingerprint as the protected `SSH_HOST_FINGERPRINT` environment secret.
- [ ] Publish `PRIVACY.md` and `TERMS.md`, verify their public URLs, and register
  the privacy URL in BotFather.
- [ ] Obtain the controller's legally required identity and jurisdictional
  contact details, complete the privacy notice, and receive legal review. These
  facts are not present in the repository and must not be invented; public
  purchase activation is blocked until this item is complete.
- [ ] Take and checksum a complete backup, restore it into an isolated database,
  run the candidate migration, and compare preflight/verification aggregates.

## Candidate deployment

1. Merge to `main`; record the full SHA from the successful build-only CD run.
2. Dispatch CD with `mode=deploy`, that exact `expected_sha`, the completed
   restorable `backup_reference`, and the exact reviewed legacy group-history
   purge count reported by preflight.
3. At the production approval gate, verify SHA, backup, image attestation, and
   expected migration head before approval.
4. Confirm CD's read-only preflight, migration, read-only verification, digest
   labels, and 60-second healthy restart-free observation all pass.
5. Keep purchases disabled and observe the candidate for at least 30 minutes.

## Telegram and telemetry smoke matrix

- [ ] English and Russian private, group, and forum chat; mention/reply behavior;
  ambient disclosure; scoped command menus; protected financial replies.
- [ ] Free-model review, consent, repeated unlimited calls, revocation, stale
  callback rejection, inline use, and group refusal of non-ZDR inference.
- [ ] Paid private/group ZDR chat with text and media; image generation/edit
  approval and delivery; Google TTS and artifact cleanup.
- [ ] Privacy/history inspection, personal deletion, ambient disable/purge, and
  policy/terms links; current Terms acceptance before invoice creation;
  `/support` and `/paysupport` intake plus operator notification.
- [ ] Operator overview, inference usage, live read-only key/catalog check, all
  maintenance passes, and command-menu synchronization.
- [ ] One real 1-Star operator checkout, exactly-once fulfillment and replay,
  confirmed **Refund latest 1-Star test**, Telegram refund update, and exact
  clawback without a second balance change.
- [ ] Logfire contains no user content or secret material, no unexplained
  exception, and no stale/unavailable inference reconciliation record.

Any privacy leak, migration mismatch, payment mismatch, missing catalog/key,
unexpected public capability, unexplained exception, or unreconciled cost is a
no-go. Provider trouble pauses affected inference surfaces; production startup
rejects an unreviewed provider downgrade.
Commerce trouble keeps or restores
`PUBLIC_PURCHASES_ENABLED=false`.

## Activation and follow-up

1. Tag the deployed candidate commit `v0.1.0`, publish the release, and verify
   that the immutable Terms and Privacy URLs resolve.
2. Set `PUBLIC_PURCHASES_ENABLED=true` and redeploy that same tagged `main` SHA through
   the production approval gate.
3. Sync Telegram command scopes from the operator console and verify public
   invoices show `2026-07-28-v1` products.
4. Record the image digest, Alembic head, backup reference, and validation
   evidence in the GitHub release.
5. Review operator and Logfire status at one hour, 24 hours, and 48 hours.
6. Preserve the verified backup and previous image until the release window is
   closed; follow `docs/deployment.md` for recovery decisions.
