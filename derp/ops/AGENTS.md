# Ops

- `observability.py` is the only module that configures logging or Logfire. Spans carry numeric IDs, kinds, counts and outcomes; never message text, captions, tool arguments, file IDs, URLs or exception messages. The one exception is `record_run_failure`, which exports a redacted, truncated exchange.
- Logfire is optional and must fail soft. Do not instrument HTTPX/aiohttp globally — Telegram file URLs contain the bot token.
- `redaction.py` masks known secrets (from `Settings.secret_values()`) and secret-shaped strings in every log line.
- `feedback.py` + `models.py` (`feedback`): `/feedback <text>` and the agent's `leave_feedback` store a person's words with kind, chat, message and run; owners review open items with `/feedbacks` in a private chat (5 per page, mark done). `forget_member` deletes a person's feedback from that chat.
- `owner.py`: best-effort plain DMs to `OWNER_IDS`.
