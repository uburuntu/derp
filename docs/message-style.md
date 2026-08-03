# User-Visible Message Style

This is the source of truth for fixed Telegram copy in English and Russian.
Generated chat answers follow the same core personality through
`derp/llm/prompts.py`, but may adapt more strongly to the user's tone.

## Voice

Derp is concise, conversational, confident, and useful. It can be witty when
the moment invites it, but it does not perform a character at the expense of
clarity.

- Prefer ordinary words and short sentences.
- Say what happened before explaining why.
- Use first person for actions Derp owns: `I couldn't send the image.`
- Use the responsible subject when it matters: `Telegram charged you.`
- Do not expose implementation terms such as operation, reconciliation,
  fulfillment, provider, model, funding, ledger, or artifact.
- Do not apologize by default. State the outcome and the useful next action.
- Do not add decorative emoji to routine status, errors, money, or privacy copy.
- Light wit belongs in conversation and onboarding, not in charge, refund,
  debt, privacy, support, or delivery-uncertainty messages. Aim for playful,
  never cute, theatrical, or cringey.
- Never leave a blocked path as prose alone. Name the reason the user can act
  on, then provide the relevant button or command in the same message.
- Prefer one stable message that edits in place over a trail of status messages.

## Information Order

Use this order when the fields apply:

1. Outcome: `I couldn't create the image.`
2. Money state: `You weren't charged.`
3. Recovery: `Try again.`

Omit recovery text when a nearby button already names the action. Never bury a
charge or refund guarantee inside an explanation.

When work is blocked, use this order:

1. Blocker: `No credits left.`
2. Available choices: `Enable free models, or add credits.`
3. Buttons: **Enable free models**, **Buy credits**.

Do not suggest an action that cannot work in the current chat or for the
current actor. A group member gets the member path; a group admin gets the
admin path.

## English

- Use contractions and conversational sentence case.
- Prefer `I couldn't...` over `The model failed...` for Derp-owned work.
- Prefer `You weren't charged.` over `Not charged.` in sentences.
- Prefer explicit buttons: `Create`, `Edit`, `Send again`, `Use my credits`,
  `Always use mine`, `Buy chat credits`, `Cancel`.
- Use `credits`, `personal credits`, and `chat credits` consistently.
- Describe Derp-owned history as `my memory`: `Delete from my memory` and
  `Deleted 12 saved messages from my memory`. Keep the legal policy precise
  about tombstones and retention.

Examples:

- `I couldn't create the image. You weren't charged.`
- `The image may already be in the chat. Check first, then tap Send again if it
  is missing. You won't be charged again.`
- `Telegram charged you, but the credits are not available yet. Don't pay
  again.`

## Russian

Russian copy is usually shorter than the English source. Translate intent, not
sentence structure.

- The persona and product name is `Дерп`, never `Derp`. Inflect it naturally
  only when a sentence cannot be rewritten cleanly in the nominative.
- First-person references are still `Дерп`: prefer `Дерп не смог отправить`
  or an impersonal concise construction over translating English `I` as `я`.
- Use a concise conversational register: `Не получилось создать изображение.`
- Avoid explicit `ты` and `вы`. Use polite plural imperatives only when an
  instruction is necessary: `Попробуйте ещё раз.`
- Prefer `Кредиты не списаны.` and `Кредиты возвращены.` as fixed money states.
- Use `личные кредиты` and `кредиты чата`; avoid `кошелёк`, `фандинг`, and
  technical accounting language.
- Use `голосовое сообщение`, not `голос`, for generated TTS output.
- Use `ё` consistently: `ещё`, `истёк`, `отменён`.
- Use gettext plurals for dynamic nouns rather than hard-coding one Russian
  grammatical form.

Examples:

- `Не получилось создать изображение. Кредиты не списаны.`
- `Изображение уже могло прийти. Сначала проверьте чат. Повторного списания не
  будет.`
- `Telegram списал Stars, но кредиты пока не зачислены. Не платите повторно.`

## Telegram Surfaces

- Button labels are commands, not descriptions, and normally contain one verb.
- Progress is a short present action: `Creating image...` / `Создаю
  изображение...`.
- Callback alerts state the changed fact, not the mechanism behind it.
- Help panels show current state and actions; they do not explain the product.
- Command usage copy is a last resort. Prefer a natural correction that says
  what input is missing.
- Destructive confirmations name the scope, what leaves Derp's memory, what
  remains in Telegram, and require a second explicit action.
- Legal documents open inside Telegram as bounded expandable pages. Opening a
  page must preserve the purchase or settings panel beneath it.
- A purchase Terms gate retains the exact product and target. Acceptance
  resumes that purchase instead of sending the user back to `/buy`.
- `/buy` is contextual: in private it opens personal products; in a group it
  first asks **For me** or **For this chat**.
- Support collects one short message. Payment and refund cases bind an exact
  receipt before the note; operator replies and refund outcomes edit the same
  stable case message.
- `/info` is a reply command. Its receipt names model, privacy mode, reported
  tokens, context size when known, and credits charged without exposing prompts
  or provider identifiers.
- In groups, a whole-word `Derp` or `дерп` anywhere in text or a caption is an
  invocation, case-insensitively. Substrings such as `derpish` are not.
- Enabling free models for a group is an admin-only, version-bound choice. The
  confirmation states that providers may retain content, and success posts a
  visible chat notice rather than hiding the change in a toast.

## Review Checklist

- Is it shorter without losing an outcome, money guarantee, or required action?
- Does it sound like the same Derp as the default system prompt?
- Is every button unambiguous without surrounding prose?
- Does every blocked state offer an action that works in this exact context?
- Does a destructive action have a confirmation and a truthful Telegram boundary?
- Does a money or support flow preserve its exact product, payment, and reason?
- Does Russian read as native concise copy rather than translated English?
- Are `Дерп`, credit terminology, `ё`, and plurals correct?
- Is the string wrapped for gettext and translated without fuzzy markers?
