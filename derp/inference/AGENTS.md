# Inference

- `roles.py` defines hidden model roles and launch pins (D28, D75). Only `chat_free` is non-ZDR; every other role sends `provider.zdr=true` per request (D66).
