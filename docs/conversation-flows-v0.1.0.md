# Derp v0.1.0 Conversation Validation

Effective 3 August 2026. This is the normative manual acceptance script for
the release candidate. It describes the intended user experience, not a ledger
of old defects. A flow that differs from this document fails validation.

Run each applicable flow with English and Russian Telegram accounts. Russian
must be shorter where possible, use `Дерп` for self-reference, and read as
native copy rather than a literal translation.

Telegram surfaces:

- **Message**: a new bot message.
- **Edited message**: one existing panel or status message changes in place.
- **Toast**: a short callback acknowledgement.
- **Alert**: a blocking callback dialog.
- **Protected DM**: a private content-protected message.

Use `{count}`, `{date}`, and `ABC123DEF0` as dynamic examples. Generated answers
are judged for relevance and the voice in `docs/message-style.md`, not exact
wording.

## 1. Private free-model onboarding

**Precondition:** private chat, no spendable credits, free models not allowed.

1. The user asks `Summarize the difference between TCP and UDP.`
2. Derp replies:

   > No credits left. Enable free models, or add credits with /buy.

   Button: **Enable free models**. The path is actionable in the same message;
   the user is not sent through `/help` first.
3. The user taps **Enable free models**. The message becomes:

   > **Allow free models?**
   >
   > Free-model providers may store prompts and replies under their own
   > policies. By continuing, you allow Derp to send prompts and replies to
   > OpenRouter and selected free-model providers under the linked Terms and
   > Privacy Policy.
   >
   > This applies to your private chat and inline mode. Group admins make a
   > separate choice for each chat.

   Buttons: **Terms**, **Privacy**, **I agree, allow free models**, **Back**.
4. **Terms** and **Privacy** each open a protected, bounded in-bot reader. Legal
   content is inside an expandable quote with **Previous**, **Next**, and
   **Back** as applicable. Closing it leaves the consent panel intact.
5. The user taps **I agree, allow free models**. Toast:

   > Free models are now allowed in private chat and inline mode.

   Russian:

   > Бесплатные модели разрешены в личном чате и инлайн-режиме.
6. Repeating the question produces a generated answer with no quota or charge
   message.

**Durable effects:** the accepted Terms and Privacy versions, preference
revision, timestamp, zero-cost operation, inference usage, and delivery receipt.
There is no daily free-request quota; every request remains individually
bounded.

## 2. Group invocation and free-model choice

**Precondition:** group or forum with no spendable chat credits and free models
off.

1. A member sends any of these ordinary messages:

   - `Could you check this, Derp?`
   - `Как думаешь, Дерп?`
   - `The answer, derp, is probably in the second paragraph.`

   A case-insensitive whole-word `Derp` or `дерп` anywhere in text or a caption
   invokes the bot. `derpish` and `антидерп` do not.
2. On the first interaction after Derp joins, it posts the chat's current
   context state before the answer or funding recovery:

   > **Derp**
   > Message me privately, or mention or reply to me in a group.
   >
   > Context: On
   > Free models: Off
   > Recent messages help me answer follow-ups. Anyone can check this setting
   > and delete their own saved messages from my memory.

   This notice appears once per chat. `Context: Off` is truthful when Telegram
   does not expose ambient messages. It never gives Derp permission to speak
   without an invocation.
3. A non-admin sees:

   > This chat is out of credits. An admin can enable free models in /settings.
   > Anyone can add chat credits with /buy.

   Button: **Open chat settings**.
4. An admin sees:

   > This chat is out of credits. Enable free models, or add chat credits with
   > /buy.

   Button: **Enable free models**.
5. The admin taps it and sees:

   > **Allow free models in this chat?**
   >
   > When chat credits are unavailable, Derp may send prompts and replies to
   > OpenRouter and selected free-model providers. Those providers may store
   > them under their own policies.
   >
   > Everyone in this chat will see a notice. Admins should also tell members
   > who join later.

   Buttons: **Terms**, **Privacy**, **Allow free models in this chat**, **Back**.
6. Only a live chat admin can confirm. After confirmation, the settings panel
   shows `Free models: On`, and Derp posts a new public message:

   > **Free models are on**
   > When chat credits are unavailable, Derp may send prompts and replies to
   > OpenRouter and selected free-model providers. Those providers may store
   > them under their policies. Admins can change this in /settings.

7. The original request can now be retried. If no free model supports its media,
   Derp says to try without the attachment or use `/buy`; it does not claim that
   free inference can handle the request.

**Durable effects:** chat-scoped policy revision, accepting admin, exact legal
versions and timestamp. Disabling the setting affects future requests. Personal
free-model consent neither enables nor disables a group.

## 3. Paid answer and `/info` receipt

**Precondition:** enough personal or chat credits for private inference.

1. On the user's first paid chat request, Derp sends this one-time notice:

   > Private models use credits. Reply to any answer with /info for the details.

2. Derp sends the generated answer without receipt spam.
3. The user replies to that answer with `/info`.
4. Derp replies with content-free facts:

   > **About this answer**
   > **Model:** {model}
   > **Privacy:** Private model
   > **Tokens:** {input} in · {output} out
   > **Context:** ~{tokens} tokens · {count} messages
   > **Charged:** {count} credits

   A free answer says `Free model`; unavailable provider token usage says `Not
   reported`; an operation not yet final says `Still settling`.
   In a group, anyone may inspect model, privacy, token, and context facts, but
   another member sees `Charged: Only the requester can see this` instead of the
   requester's charge.
5. `/info` without replying to a Derp answer returns:

   > Reply to one of my answers with /info.

**Durable effects:** a content-free mapping from the delivered Telegram answer
to operation, model display name, privacy mode, context counts, usage, and final
credit state. Prompts and answers are not copied into the receipt.

## 4. Contextual purchase and in-bot Terms

**Precondition:** purchases enabled and current Terms not accepted.

1. In private, `/buy` opens personal top-ups and the personal plan.
2. In a group, `/buy` first asks:

   > **Buy credits**
   > Who are they for?

   Buttons: **For me**, **For this chat**. The menu is bound to the actor who
   opened it. `/buy_chat` redirects to this contextual `/buy` flow.
3. The user chooses a target and a product. If Terms are not current, Derp sends
   a protected panel:

   > **Terms and privacy**
   > Accept these terms before buying credits or a plan.

   Buttons: **Terms of use**, **Privacy policy**, **Accept terms**.
4. Each legal button opens complete localized pages inside Telegram without
   replacing the purchase panel.
5. The user taps **Accept terms**. The panel says:

   > Terms accepted. Continuing your purchase…

   Derp resumes the exact product and personal/chat target. The user does not
   reopen `/buy` or make the selection again.
6. Derp presents the exact invoice summary and **Pay {price}**. Telegram's native
   checkout performs the final confirmation.
7. A successful payment reports the granted credits or active plan. A delayed
   or review-required payment says not to pay again and links to `/support`.

**Durable effects:** current legal acceptance, exact actor/target/product
continuation, opaque purchase intent, pre-checkout decision, pre-ack payment
update, receipt, wallet lot or plan cycle, and independent result-message retry.

## 5. One-message support and exact refunds

**Precondition:** user and operator each use their private chat with Derp.

1. `/support` shows:

   > **Support**
   > Choose a topic, then send one short message.

   Buttons: **Payment or credits**, **Refund**, **Privacy or data**, **Access or
   account**, **Terms of use**, **Privacy policy**.
2. In a group, `/support` does not collect details publicly. It says:

   > Support notes and payment details stay private.

   Button: **Open support**, leading directly to private support.
3. For **Payment or credits** or **Refund**, Derp first shows the user's recent
   receipts as `{stars} Stars · {credits} · {date}`. The user selects the exact
   payment. Payment support also offers **Something else**; refund does not
   invent a refundable payment when none exists.
4. Derp sends a protected ForceReply prompt:

   > What happened? One message is enough.

   The user replies once, up to 800 characters.
5. That prompt edits in place to the stable receipt:

   > **Support case open**
   > Refund: `ABC123DEF0`
   >
   > I have your note. An operator will review it here.

   Button: **Refresh**. It re-renders the same message from durable state if a
   Telegram edit was missed.

6. In `/operator` -> **Support**, the operator can page through cases, open one,
   read the note and exact payment facts, then choose **Refund**, **Reply and
   close**, or **Decline**.
7. **Reply and close** and **Decline** require one short operator reason. The
   user's stable case message edits to the final status and exact reason.
8. **Refund** submits the selected receipt through the restart-safe refund path.
   The same stable message becomes either `Refund complete` or `Refund requested.
   Telegram is processing it.` A provider rejection leaves the case open.

**Durable effects:** one bounded user note, category, source, exact optional
payment receipt, stable status-message coordinate, operator decision and reason,
and exact refund request. Support notes are excluded from conversation history
and inference. The user note and operator decision text are purged 30 days after
closure; case status, reference, relationships, timestamps, and accounting facts
remain.

## 6. Privacy deletion and context cleanup

1. `/privacy` shows the retention period and says:

   > You can delete your own saved messages from my memory at any time.

   Primary button: **Delete from my memory**.
2. Tapping it requires a second step:

   > **Delete your saved messages from my memory?**
   > This deletes Derp's saved messages and media references. Telegram messages
   > and copies made by other people remain.

   Buttons: **Delete**, **Cancel**.
3. Success returns to the privacy panel and reports:

   > Deleted {count} saved messages from my memory

4. `/forget`, sent as a reply to one of the user's own messages, removes that
   one saved copy from Derp's memory.
5. A group admin choosing **Turn context off** first sees:

   > **Clean up my memory?**
   > This turns context off and deletes ambient messages saved from this whole
   > chat, including every topic, from my memory. Mentions and replies may still
   > be saved. Telegram messages stay.

   Buttons: **Clean up memory**, **Cancel**.
6. Confirmation deletes ambient history across the whole group and reports the
   count removed. Explicit mentions and replies remain eligible for retention.

**Durable effects:** personal deletion anonymizes owned content and attachment
references until its retention deadline; ambient cleanup hard-deletes ambient
history and expires affected deferred work. Telegram content and separately
retained billing, consent, support, and security records remain outside this
conversation-history action.

## 7. Paid image approval

1. A generated image or edit is never started from an opaque yes/no question.
   Before provider work, Derp replies with:

   > **Generate image?**
   > {count} credits · {model}
   > Private · zero-data retention
   > **Prompt:** {bounded preview}
   > Style: Automatic

   Buttons: **Generate**, **Change style**, **Buy credits** when purchases are
   available, and **Cancel**. An edit uses **Edit image?** and **Apply edit**.
2. **Change style** edits the control to **Automatic**, **Photo**,
   **Illustration**, and **Cinematic** choices. Choosing one creates a fresh
   quote and returns to the approval with its updated price; it does not charge
   or run the old request.
3. Only the original requester in the original chat and topic can approve it.
   Forwarded, stale, expired, or already-used controls fail closed.
4. **Generate** runs once. A successful delivery captures the quoted credits;
   cancellation or a pre-delivery failure releases them and states that no
   charge was made.

**Durable effects:** authenticated approval capability, immutable quote,
request binding, reservation/capture/release events, generated artifact, and
delivery state. Prompts are not copied into pricing metadata.

## 8. Unknown command recovery

1. The user sends a typo such as `/settngs`.
2. Derp replies without inference:

   > I don't know that command. Try /help.

3. The typo and recovery message are excluded from conversation history.
   Commands intentionally handled by chat, including `/derp`, keep their normal
   conversational route.

## 9. Release pass criteria

- Every blocked state offers a valid action for that actor and chat.
- English is concise and conversational. Russian is tighter, uses `Дерп`, and
  contains no untranslated or fuzzy production entry.
- Money messages state charge, refund, debt, or no-charge status before recovery.
- Privacy choices are version-bound, admin/user scoped, and visibly disclosed.
- Destructive actions require confirmation and name what remains in Telegram.
- Legal pages are complete in Telegram; purchase acceptance resumes exactly once.
- Support binds the intended payment, persists one note, and returns the exact
  operator reason or refund state in the original case message.
- `/info` reveals useful accounting facts without revealing content or internal
  provider identifiers.
- Stale or forwarded buttons fail closed without changing money, privacy, or
  support state.
