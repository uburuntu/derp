# Migrations

- Generate with `make revision MSG="..."` against an up-to-date database, then review: keep extensions, generated columns and partial/GIN indexes explicit.
- Tests build the schema only from these files; `tests/test_migrations.py` enforces a single head and zero drift from `derp.db.metadata`.
- Until the v2 cutover revisions may be reshaped freely. After cutover they are expand-only: add nullable columns/tables first, deploy writers, backfill, and only drop in a later release once no running code reads the old shape.
- `alembic upgrade head` runs in the deploy before the new container starts (S2).
