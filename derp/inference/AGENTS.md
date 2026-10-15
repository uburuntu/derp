# Inference

- `roles.py` defines hidden model roles and launch pins; pins are a starting point, not a user-facing choice. Only `chat_free` is non-ZDR; every other role sends `provider.zdr=true` per request (no account-level ZDR toggle, it would block free models).
