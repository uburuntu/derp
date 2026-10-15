# Locales

- `en.toml` and `ru.toml` hold every user-facing string; code calls `t(locale, key, **values)`. Keys and `{placeholders}` must match across catalogs (enforced by `tests/locales`).
- Locale choice: public group messages use the chat locale; messages addressed to one person use their `language_code`; generated answers mirror the conversation instead.

## Voice

- Persona copy is chaotic but brief: playful, meme-aware, occasionally absurd, never corporate; 1–4 lines in groups. Russian self-name is «Дерп».
- Money, privacy and error copy is plain and calm: outcome first, then the money state ("not charged", "12 dc left"), then the recovery step.
- Russian avoids choosing ты/вы: prefer impersonal or infinitive phrasing ("Можно пополнить через /buy").
- Currency: Derpcoins / дерпкоины, short `dc`. The paid plan is Derp+.
