# Derp v0.1.0 conversation-flow review

This is a manual acceptance script for the Telegram behavior currently
implemented on `chore/modernize-runtime-foundations`. It describes observed
behavior, including awkward transitions; it is not an idealized design.

Run every applicable flow once with an English Telegram account and once with a
Russian account. Text marked `[generated answer]` is model output and should be
reviewed for tone and relevance rather than exact wording.

Telegram surfaces are named explicitly:

- **Message**: a new bot message in the chat.
- **Edited message**: an existing panel or progress message changes in place.
- **Toast**: the short callback acknowledgement at the bottom of Telegram.
- **Alert**: a blocking Telegram callback dialog.
- **Protected DM**: a private message sent with content protection enabled.

Dynamic examples use `{date}`, `{count}`, and `ABC123DEF0` as placeholders.
Current default quotes are 102 credits for a 1K image, 33 credits for TTS's
30-second ceiling, and 116/254/802 credits for small/medium/large paid chat
context bands.

## 0. Candidate purchase gate

**Precondition:** `PUBLIC_PURCHASES_ENABLED=false`, as required during candidate
deployment and burn-in.

1. User types `/buy` or `/buy_chat`.
2. Derp sends a message with no buttons:

   > Credit purchases are temporarily unavailable. You won't be charged. Your
   > existing credits and free features still work.

   Russian:

   > Покупка кредитов временно недоступна. Telegram не спишет Stars. Имеющиеся
   > кредиты и бесплатные функции по-прежнему доступны.

3. No purchase intent or invoice is created. Existing credits, reconciliation,
   and free inference remain available.

**Pass:** no stale purchase button can reach Telegram checkout while intake is
closed.

Sources: `derp/handlers/credit_cmds.py`,
`derp/credits/purchase_suspension.py`.

## 1. New private user enables free models

**Precondition:** private chat, no credits, no free-model consent.

1. User types `/start`.
2. Derp replies, with no buttons:

   > Hi, Alex. Ask me anything here, or open /help for settings and tools.

   Russian:

   > Привет, Алекс. Здесь можно спросить о чём угодно. Для настроек и
   > возможностей отправьте /help.

3. User asks `Summarize the difference between TCP and UDP.`
4. Derp replies, with no buttons:

   > No free model is available here. Enable free models in a private chat, or
   > add credits for private models.

   Russian:

   > Бесплатная модель здесь недоступна. Включите бесплатные модели в личном
   > чате или пополните баланс для приватных.

5. User types `/settings`, then taps **Model privacy**.
6. The settings message is edited to:

   > **Model privacy**  
   > Mode: Private
   >
   > Derp uses zero-data-retention models. Free models are optional and may let
   > providers store prompts and replies. They can run only in private chat and
   > inline mode. Groups stay private.

   Buttons: **Review free-model terms**, **Back**.

7. User taps **Review free-model terms**.
8. The message is edited to:

   > **Allow free models?**
   >
   > Free-model providers may store prompts and replies under their own
   > policies. By continuing, you allow Derp to send prompts and replies to
   > OpenRouter and selected free-model providers under the linked Terms and
   > Privacy Policy.
   >
   > This applies only in private chat and inline mode. Groups stay private.

   Buttons: **Terms**, **Privacy**, **I agree, allow free models**, **Back**.

9. User reviews both links and taps **I agree, allow free models**.
10. The message returns to **Model privacy**, now showing `Mode: Free models
    allowed`. The action is **Use private models only**. Toast:

    > Free models are now allowed in private chat and inline mode.

    Russian toast:

    > Бесплатные модели разрешены в личном чате и инлайн-режиме.

11. User repeats the TCP/UDP question. Telegram shows typing, then Derp replies
    with `[generated answer]`; there are no buttons or charge message.

**Durable effects:** versioned non-ZDR consent, inference usage, and the normal
private history record. Free requests have no daily admission limit but remain
bounded per request. The consent never authorizes group inference.

Sources: `derp/handlers/basic.py`, `derp/handlers/context_settings.py`,
`derp/handlers/chat.py`.

## 2. Paid private chat and failure

**Precondition:** private chat with enough personal credits.

1. User sends `Read this PDF and give me the three main risks.` with a PDF.
2. Telegram shows typing.
3. Derp replies to the request with `[generated answer]`.
4. No price, confirmation, buttons, remaining balance, or charge receipt is
   shown in the conversation.
5. User may type `/credits` to inspect the charge under **Recent activity**.

Failure branch:

1. If provider or delivery fails definitively, Derp replies:

   > I couldn't answer that. You weren't charged. Try again.

   Russian:

   > Не получилось ответить. Кредиты не списаны. Попробуйте ещё раз.

**Durable effects:** the quote and reservation precede inference. Credits are
captured only after Telegram acknowledges delivery; a definite failure releases
the reservation.

Sources: `derp/handlers/chat.py`, `derp/features/chat_accounting.py`,
`derp/operations/quotes.py`.

## 3. Group mention, history notice, and funding

**Precondition:** group or forum; run once with chat credits and once without.

1. User types `Derp, summarize the decisions above.` The same handler is
   triggered by `/derp`, `Дерп`, a reply to a Derp message, or any whole-word
   `derp`/`дерп` in ordinary text.
2. On the first handled interaction, Derp may first send the full settings
   panel:

   > **Derp**  
   > Message me privately, or mention or reply to me in a group.
   >
   > Context: On · 30 days  
   > Recent messages help me answer follow-ups. Anyone can check this setting
   > and delete their own saved messages.

3. Derp then replies with `[generated answer]`.
4. Funding order is chat credits first. Personal credits are considered only
   after the user has enabled persistent consent for this chat.
5. Typing `/credits` in the group produces only this public acknowledgement:

   > I sent your credit details in a private chat.

   The balance and activity arrive as a protected DM. In that DM the user can
   tap **Use my credits without asking** or **Ask before using my credits**.

No-funding branch:

1. Derp replies, with no recovery buttons:

   > No free model is available here. Enable free models in a private chat, or
   > add credits for private models.

2. Private free-model consent does not change this result: groups always use
   reviewed private routes.

**Durable effects:** explicit invocations are retained. Ambient group messages
are retained only when context is enabled. The first-use notice records that it
was shown.

Sources: `derp/filters/derp_mention.py`, `derp/handlers/chat.py`,
`derp/handlers/context_settings.py`, `derp/handlers/credit_cmds.py`.

## 4. Image and voice approval

### Image

1. User types `/imagine a red bicycle in the rain`.
2. Derp replies:

   > Create this image for 102 credits?

   Buttons: **Create image**, **Cancel**.

   Russian:

   > Создать это изображение за 102 кредита?

   Buttons: **Создать изображение**, **Отмена**.

3. User taps **Create image**. Toast: `Started`. The approval message is edited
   to `Creating your image...`.
4. On success, a Telegram photo replies to the original request and the approval
   message is deleted.

Cancellation:

> Canceled. You weren't charged.

Uncertain delivery:

> The image may already be in the chat. You won't be charged again. Check first,
> then tap Send again if it's missing.

Button: **Send again**. A resend cannot charge again.

Group funding branch:

> This chat can't cover the image. You weren't charged. Use your credits once
> or add chat credits.

Buttons: **Use my credits once**, **Always use my credits**, optionally
**Buy chat credits**, and **Try again**.

### Voice

1. User types `/tts Read this aloud`.
2. Derp replies:

   > Create this voice message for 33 credits?

   Buttons: **Create voice**, **Cancel**.

   Russian:

   > Создать это голосовое сообщение за 33 кредита?

3. After approval, the same message cycles through `Preparing your voice
   message...`, `Creating your voice message...`, and `Sending your voice
   message...`.
4. On success, a Telegram voice message replies to the command and the control
   message is deleted.

**Durable effects:** immutable quote, actor/chat-bound approval, reservation,
inference usage, private artifact, delivery attempt, and capture or reversal.
Definite terminal delivery failure returns credits.

Sources: `derp/handlers/image.py`, `derp/handlers/tool_approvals.py`,
`derp/handlers/tts.py`, `derp/handlers/paid_media_controls.py`.

## 5. Terms, top-up, and payment recovery

**Precondition:** private chat, purchases enabled, current Terms not accepted.

1. User types `/buy`.
2. Derp sends:

   > **Buy personal credits**  
   > Choose an option. Telegram asks you to confirm before charging.

   Buttons:

   - **600 credits · 50 Stars** | **3200 credits · 250 Stars**
   - **9900 credits · 750 Stars**
   - **Derp Personal · 500 Stars / 30 days**

3. User taps **600 credits · 50 Stars**.
4. Derp sends a protected message:

   > **Terms and privacy**  
   > Accept these terms before buying credits or a plan.

   Buttons: **Terms of use**, **Privacy policy**, **Accept terms**. Alert:

   > Review and accept the current terms before buying.

5. User taps **Accept terms**. The protected message edits to:

   > **Terms and privacy**  
   > Accepted for purchases

   The accept button disappears. Toast: `Terms accepted`.
6. Current behavior does not resume checkout. User must type `/buy` and select
   **600 credits · 50 Stars** again.
7. Derp sends:

   > **600 credits for you**  
   > Telegram will ask you to confirm before charging 50 Stars.

   Button: **Pay 50 Stars**. Toast: `Invoice ready`.
8. The button opens Telegram's native Stars checkout. After confirmation, Derp
   sends:

   > **Payment complete**  
   > Purchased credits available: 600

Delayed settlement:

> Payment recorded. Credits are still processing. Don't pay again; check
> /credits shortly.

Review state:

> Telegram charged this payment, but no credits were added because the details
> need review. Don't pay again. Open /paysupport.

Group variant:

- `/buy_chat` shows the same three top-ups, without the subscription.
- The selector and invoice link are public in the group.
- On success, exact balance details arrive by protected DM; the group sees only
  `I sent the payment details in a private chat.`

**Durable effects:** accepted legal version, opaque payer/target-bound intent,
pre-checkout state, pre-ack update inbox, receipt, wallet lot, and independent
reply recovery.

Sources: `derp/billing/telegram.py`, `derp/handlers/legal_support.py`,
`derp/handlers/payments.py`, `derp/billing/payment_updates.py`.

## 6. Personal plan, cancellation, and refund

**Precondition:** purchases enabled and Terms accepted.

1. In `/buy`, user taps **Derp Personal · 500 Stars / 30 days** and completes
   Telegram checkout.
2. Derp sends:

   > **Plan active**  
   > 6750 monthly credits are available.

3. User types `/plan`.
4. Derp sends:

   > **Derp Personal**  
   > Active · renews automatically
   >
   > Your current credits are available until {date}.

   Button: **Cancel automatic renewal**.
5. User taps the button. The panel edits to:

   > **Derp Personal**  
   > Active · renewal off
   >
   > Your credits remain available until {date}.

   Button: **Turn renewal back on**. Toast:

   > Automatic renewal is off. Your paid period stays active.

Provider-pending branch:

> Telegram hasn't confirmed this yet. Derp will retry.

Russian:

> Telegram пока не подтвердил. Дерп повторит.

Refund behavior:

- `/support` → **Refund** only opens a support case; it does not initiate a
  refund.
- Balance changes begin when Telegram sends a refund update.
- Derp then sends `Refund complete` with unused credits removed. If some value
  was already spent, it also says `Payment debt added: {count} credits. Paid
  features are paused.`
- A refund of the current plan cycle expires the local entitlement and stages
  cancellation of future renewal.

**Durable effects:** non-rolling allowance cycle, leased absolute renewal
command, exact-source clawback, debt provenance, and bounded replay.

Sources: `derp/handlers/subscriptions.py`, `derp/billing/subscriptions.py`,
`derp/billing/settlement.py`, `derp/handlers/payments.py`.

## 7. Privacy deletion and group context

1. User types `/privacy`.
2. Derp sends:

   > **Privacy and history**  
   > Saved messages are deleted after 30 days.
   >
   > You can delete your own saved messages at any time. Chat admins can clear
   > this chat or topic. Approved shared facts are kept separately.

   Buttons for everyone: **Delete my messages**, **Privacy policy**, **Terms of
   use**, **Contact support**, **Back**. Admins also see **Clear this chat/topic**
   and **Forget shared facts**.

3. User taps **Delete my messages**.
4. The message is edited to:

   > **Delete your saved messages from this chat?**  
   > This deletes Derp's saved messages and media references. Telegram messages
   > and copies made by other people remain.

   Buttons: **Delete**, **Cancel**.
5. User taps **Delete**. The privacy panel returns. Toast:

   > Deleted 12 saved messages

   Russian:

   > Удалено 12 сохранённых сообщений

Group-admin branch:

1. Admin opens `/settings` and taps **Turn context off**.
2. There is no confirmation. The panel becomes `Context: Off · 30 days`. Toast:

   > Context is off · 12 saved messages deleted

3. Ambient history is deleted immediately. Explicit mentions and replies to
   Derp can still be retained and handled.

**Durable effects:** personal deletion tombstones owned content and media
references in the current chat. Admin context disable hard-deletes ambient chat
history. `/forget` can target one replied-to message owned by the requester.

Sources: `derp/handlers/context_settings.py`, `derp/db/history.py`,
`derp/history/policy.py`.

## 8. Support case and operator resolution

**Precondition:** user and operator are both in their private chats with Derp.

1. User types `/support`.
2. Derp sends a protected message:

   > **Support**  
   > Choose the closest category. Derp stores the category and case reference,
   > not a free-form support message.

   Buttons: **Payment or credits**, **Refund**, **Privacy or data**, **Access or
   account**, **Terms of use**, **Privacy policy**.
3. User taps **Refund**. The panel gains:

   > Open cases  
   > Refund: `ABC123DEF0`

   Toast: `Case opened`. Repeating the action gives `Case already open`.
4. Operators receive:

   > **New support case**  
   > Type: Refund  
   > Reference: `ABC123DEF0`

5. In `/operator` → **Support**, an operator sees the category, reference,
   creation time, and requester Telegram ID. They tap **Resolve ABC123DEF0**.
6. Confirmation panel:

   > **Resolve support case?**  
   > `ABC123DEF0` · Refund  
   > {date} · user `123456789`

   Buttons: **Resolve case**, **Cancel**.
7. After confirmation, the user receives:

   > **Support case resolved**  
   > Refund: `ABC123DEF0`

8. The operator queue shows `Case resolved. The requester was notified.` If the
   user notification fails, the case remains open.

**Durable effects:** category-only case and opaque reference, deduplication per
category, operator-bound single-use confirmation, notify-before-close state.
No user-authored explanation or operator resolution text is stored or sent.

Sources: `derp/handlers/legal_support.py`, `derp/support/service.py`,
`derp/handlers/operator.py`.

## Findings before activation

### Blockers

1. **The legal links currently fail.** Terms, Privacy, and model-consent buttons
   point to the GitHub tag `v0.1.0`, which does not exist locally or on the
   remote. The release sequence also asks testers to verify these links before
   creating that tag.
2. **Support cannot carry enough information to resolve a real issue.** The
   user can send only a category. The operator cannot request details, attach a
   payment to the case, or send resolution text; the final user message repeats
   only the category and reference.
3. **Group purchase Terms acceptance is unusable in place.** A group purchase
   posts the Terms panel in the group, but the accept button is private-only.
   The user must discover private `/terms`, then return and restart
   `/buy_chat`.

### High-risk UX decisions

1. Accepting Terms from the private purchase gate is a dead end. Checkout does
   not resume, and the accepted panel has no **Continue purchase** or **Back to
   shop** action.
2. `/buy` is accepted in groups and publicly reveals a personal pack selection
   and payer-bound invoice link. Only settlement details are private.
3. Free onboarding invites the user to ask anything, then rejects the first
   question without an **Enable free models** action.
4. Group funding failure suggests enabling free models even though free models
   can never serve a group. It provides no direct credit or consent button.
5. Any whole-word `derp`/`дерп` in group conversation can invoke a paid request,
   even without a Telegram mention or reply.
6. Paid chat has no pre-run price or post-run receipt. A natural-language image
   request may charge both the chat turn and the image while the approval shows
   only the image quote.
7. Image approval does not show the prompt/style being approved. Private image
   insufficient-funds recovery says to add credits but offers only **Try again**,
   not **Buy credits**.
8. **Turn context off** has no confirmation and does not explain that explicit
   mentions/replies may still be stored. In forums, clearing “this chat” outside
   a topic does not clear every topic.
9. **Contact support** has no **Back** action, the queue shows only the oldest
   eight cases, and support-resolution delivery can duplicate after a crash
   between notification and close.

Do not mark the Telegram smoke matrix complete until the three blockers are
resolved and each high-risk item is explicitly accepted or changed.
