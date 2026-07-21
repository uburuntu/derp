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
- Light wit belongs in conversation, onboarding, and donation thanks, not in
  charge, refund, debt, or delivery-uncertainty messages.

## Information Order

Use this order when the fields apply:

1. Outcome: `I couldn't create the image.`
2. Money state: `You weren't charged.`
3. Recovery: `Try again.`

Omit recovery text when a nearby button already names the action. Never bury a
charge or refund guarantee inside an explanation.

## English

- Use contractions and conversational sentence case.
- Prefer `I couldn't...` over `The model failed...` for Derp-owned work.
- Prefer `You weren't charged.` over `Not charged.` in sentences.
- Prefer explicit buttons: `Create`, `Edit`, `Send again`, `Use my credits`,
  `Always use mine`, `Buy chat credits`, `Cancel`.
- Use `credits`, `personal credits`, and `chat credits` consistently.

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
- Destructive confirmations name the scope and the irreversible effect.

## Review Checklist

- Is it shorter without losing an outcome, money guarantee, or required action?
- Does it sound like the same Derp as the default system prompt?
- Is every button unambiguous without surrounding prose?
- Does Russian read as native concise copy rather than translated English?
- Are `Дерп`, credit terminology, `ё`, and plurals correct?
- Is the string wrapped for gettext and translated without fuzzy markers?
