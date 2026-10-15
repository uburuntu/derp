# Ops

- `observability.py` is the only module that configures logging or Logfire. Spans carry numeric IDs, kinds, counts and outcomes; never message text, captions, tool arguments, file IDs, URLs or exception messages. The one exception is `record_run_failure`, which exports a redacted, truncated exchange (D16).
- Logfire is optional and must fail soft (D113). Do not instrument HTTPX/aiohttp globally — Telegram file URLs contain the bot token.
- `redaction.py` masks known secrets (from `Settings.secret_values()`) and secret-shaped strings in every log line.
