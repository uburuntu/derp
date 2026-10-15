# Database plumbing

- `base.py` owns the declarative `Base`: constraint naming convention, BIGINT for every `int` (Telegram IDs), timezone-aware timestamps, JSONB for dict/list columns.
- `engine.py` owns `Database`: one engine per process; units of work open short sessions (`session()` for reads, `transaction()` for writes). Never hold a session across Telegram or provider calls.
- `metadata.py` imports every domain `models` module; add new model modules there or Alembic and the drift test will not see them.
- Models live in their domain package (`derp/<domain>/models.py`). Invariants that money or privacy depends on are DB `CHECK`s, not only Python validation.
