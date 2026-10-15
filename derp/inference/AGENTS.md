# Inference

- `roles.py` defines hidden model roles and launch pins; users never choose models. Only `chat_free` is non-ZDR; every other role sends `provider.zdr=true` per request (an account-level ZDR toggle would also block the free models).
- `openrouter.py` is the provider seam: pin → Pydantic AI `OpenRouterModel` + per-request settings (fallbacks via `openrouter_models`, ZDR, usage accounting, prompt caching, reasoning effort). Reasoning `"none"` is sent as `{"enabled": false}` because some providers ignore effort `none`.
- Cost is always OpenRouter's reported `usage.cost` (`provider_details["cost"]`, includes web-search fees). Never use `ModelResponse.cost()` (static price table). Cancelled streams: look the cost up with `OpenRouterAPI.generation`.
- `api.py` covers what Pydantic AI does not: Images API (no zdr flag → ZDR hosts pinned with `provider.only` per model), speech-to-text, catalog/endpoints for the model scout, generation lookups.
- A second provider would add a sibling module behind the same role → model/settings/usage functions; nothing outside this package imports OpenRouter types.
- `tests/live` holds opt-in smoke tests against the real API (`pytest -m live`, fractions of a cent).
- `models.py`/`ledger.py`: `provider_calls` is the single ledger of every paid provider call (chat turns, STT, images, digests, scout probes) with who funded it; metering, `/stats` and reconciliation read from it.
- `pins.py`: live pins live in `model_pins` (seeded from settings on startup, never overwritten by the seed); `PinStore.get(role)` caches for 5 minutes. Every pin change keeps the previous pin for one-step rollback.
- `scout.py` runs daily: auto-applies only safe changes (dead primary → first live fallback, price snapshot of the cheapest live ZDR route); smarter-or-cheaper challengers must pass a tiny live smoke check (Russian answer, tool call) and become `scout_proposals` for the owner to apply or ignore.
