# ruff: noqa: S608 -- interpolated SQL identifiers come exclusively from AMOUNTS and CHECKS below.
"""fractional derpcoins

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-21 13:33:38.343187
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Original BIGINT columns remain for the whole-coin release during deployment. The precise
# columns use the same unit (one Derpcoin). Every pg trigger runs in its caller's transaction.
AMOUNTS = {
    "agent_runs": ("charged_credits",),
    "approvals": ("credits",),
    "credit_lots": ("amount", "remaining"),
    "ledger_entries": ("amount",),
    "reservation_holds": ("amount",),
    "reservations": ("amount", "captured"),
    "run_fees": ("credits",),
}
CHECKS = {
    "agent_runs": ("precise_charge_nonnegative", "charged_credits_dc >= 0"),
    "approvals": ("precise_credits", "credits_dc >= 0"),
    "credit_lots": ("precise_remaining_in_range", "amount_dc > 0 AND remaining_dc BETWEEN 0 AND amount_dc"),
    "ledger_entries": ("precise_amount_positive", "amount_dc > 0"),
    "reservation_holds": ("precise_amount_positive", "amount_dc > 0"),
    "reservations": ("precise_captured_in_range", "amount_dc > 0 AND captured_dc BETWEEN 0 AND amount_dc"),
    "run_fees": ("precise_credits", "credits_dc >= 0"),
}


def _bridge(table: str, columns: tuple[str, ...]) -> None:
    # The whole-coin writer can continue unmodified before fractional billing starts. Refuse
    # an old writer mutating a fractional amount: rounding such a hold or refund loses money.
    parts = []
    if table == "ledger_entries":
        # Old callers can otherwise credit back a rounded-up fractional hold after rolling back a
        # release. All monetary movements journal under the wallet lock, so reject the entire old
        # transaction once this wallet has fractional history.
        parts.append("""
            IF TG_OP = 'INSERT' AND NEW.amount_dc IS NULL AND EXISTS (
                SELECT 1 FROM ledger_entries WHERE wallet_id = NEW.wallet_id AND amount_dc <> amount
            ) THEN RAISE EXCEPTION 'fractional wallet requires the decimal-aware writer'; END IF;
        """)
    for name in columns:
        exact = f"{name}_dc"
        round_to_whole = "floor" if name == "remaining" else "ceil"
        parts.append(f"""
            IF NEW.{exact} IS NULL THEN
                NEW.{exact} := NEW.{name};
            ELSIF TG_OP = 'UPDATE' AND NEW.{name} IS DISTINCT FROM OLD.{name}
                    AND NEW.{exact} IS NOT DISTINCT FROM OLD.{exact} THEN
                IF OLD.{exact} <> trunc(OLD.{exact}) THEN
                    RAISE EXCEPTION 'fractional wallet requires the decimal-aware writer';
                END IF;
                NEW.{exact} := NEW.{name};
            END IF;
            NEW.{name} := {round_to_whole}(NEW.{exact})::bigint;
        """)
    op.execute(f"""CREATE OR REPLACE FUNCTION derp_dc_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN {"".join(parts)} RETURN NEW; END $$""")
    op.execute(f"""CREATE OR REPLACE TRIGGER sync_derpcoins BEFORE INSERT OR UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION derp_dc_{table}()""")


def upgrade() -> None:
    for table, fields in AMOUNTS.items():
        for name in fields:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name}_dc numeric(25,6)")
        _bridge(table, fields)
    # Historical ledger movements are immutable, including while filling the added column.
    # Permit precisely one backfill of the new field with its pre-existing value. Deletes and
    # every change to any previously stored value still raise in the original trigger.
    op.execute("""CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.amount_dc IS NULL AND NEW.amount_dc = OLD.amount
               AND (to_jsonb(OLD) - 'amount_dc') = (to_jsonb(NEW) - 'amount_dc') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'ledger_entries is append-only';
        END $$""")
    with op.get_context().autocommit_block():
        op.execute("""CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ledger_fractional_wallet
            ON ledger_entries(wallet_id) WHERE amount_dc <> amount""")
        for table, fields in AMOUNTS.items():
            first = fields[0]
            assignments = ", ".join(f"{f}_dc = {f}" for f in fields)
            # Walk the existing primary index once, committing each batch. The bridge
            # initializes concurrent inserts and updates, even behind our cursor.
            keys = ("reservation_id", "lot_id") if table == "reservation_holds" else ("id",)
            order = ", ".join(keys)
            descending = ", ".join(f"{col} DESC" for col in keys)
            bound = ", ".join(f":{col}" for col in keys)
            cursor = {}
            while True:
                after = f" AND ({order}) > ({bound})" if cursor else ""
                batch = text(f"""
                    WITH todo AS (
                        SELECT {order} FROM {table} WHERE {first}_dc IS NULL{after} ORDER BY {order} LIMIT 5000
                    ), filled AS (
                        UPDATE {table} SET {assignments} WHERE {first}_dc IS NULL
                            AND ({order}) IN (SELECT {order} FROM todo)
                    )
                    SELECT {order} FROM todo ORDER BY {descending} LIMIT 1
                """)
                last = op.get_bind().execute(batch, cursor).mappings().first()
                if last is None:
                    break
                cursor = dict(last)
            required = " AND ".join(f"{f}_dc IS NOT NULL" for f in fields)
            name, condition = CHECKS[table]
            for constraint, expr in ((f"ck_{table}_{name}", condition), ("derp_dc_present", required)):
                op.execute(f"""DO $$ BEGIN
                    ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({expr}) NOT VALID;
                    EXCEPTION WHEN duplicate_object THEN NULL; END $$""")
                op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
            for col in fields:
                op.execute(f"ALTER TABLE {table} ALTER COLUMN {col}_dc SET NOT NULL")
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT derp_dc_present")

    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM credit_lots l LEFT JOIN (
                SELECT lot_id, sum(amount_dc) FILTER (WHERE kind = 'credit') AS issued,
                    sum(CASE kind WHEN 'credit' THEN amount_dc WHEN 'release' THEN amount_dc
                    WHEN 'capture' THEN 0 ELSE -amount_dc END) AS remaining
                FROM ledger_entries GROUP BY lot_id
            ) e ON e.lot_id = l.id WHERE l.amount_dc IS DISTINCT FROM e.issued
                                      OR l.remaining_dc IS DISTINCT FROM e.remaining
        ) OR EXISTS (
            SELECT 1 FROM reservations r LEFT JOIN (
                SELECT reservation_id, sum(amount_dc) AS held FROM reservation_holds GROUP BY reservation_id
            ) h ON h.reservation_id = r.id WHERE r.amount_dc IS DISTINCT FROM h.held
        ) OR EXISTS (
            SELECT 1 FROM (
                SELECT r.wallet_id, r.operation_id, h.lot_id, h.amount_dc, r.state
                FROM reservation_holds h JOIN reservations r ON r.id = h.reservation_id
            ) h FULL JOIN (
                SELECT wallet_id, operation_id, lot_id, sum(amount_dc) FILTER (WHERE kind = 'hold') AS held,
                    coalesce(sum(amount_dc) FILTER (WHERE kind IN ('capture','release')), 0) AS settled
                FROM ledger_entries WHERE kind IN ('hold','capture','release') GROUP BY wallet_id, operation_id, lot_id
            ) e USING (wallet_id, operation_id, lot_id)
            WHERE h.amount_dc IS DISTINCT FROM e.held OR e.settled <> CASE WHEN h.state='held' THEN 0 ELSE h.amount_dc END
        ) OR EXISTS (
            SELECT 1 FROM reservations r LEFT JOIN (
                SELECT operation_id, sum(amount_dc) AS captured FROM ledger_entries
                WHERE kind = 'capture' GROUP BY operation_id
            ) e USING (operation_id) WHERE r.captured_dc <> coalesce(e.captured, 0)
        ) THEN RAISE EXCEPTION 'Derpcoin reconciliation failed; keep the current poller'; END IF;
    END $$""")


def downgrade() -> None:
    # The migration is additive. A previous app may take over before any fractional money has
    # moved. After that point the rollback target must understand fractions.
    for table, fields in AMOUNTS.items():
        fractional = " OR ".join(f"{f}_dc <> {f}" for f in fields)
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT 1 FROM {table} WHERE {fractional}) THEN
            RAISE EXCEPTION 'cannot downgrade fractional Derpcoin history'; END IF; END $$""")
    op.drop_index("ix_ledger_fractional_wallet", table_name="ledger_entries")
    for table, fields in reversed(AMOUNTS.items()):
        op.execute(f"DROP TRIGGER sync_derpcoins ON {table}")
        op.execute(f"DROP FUNCTION derp_dc_{table}()")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_{CHECKS[table][0]}")
        for col in fields:
            op.drop_column(table, f"{col}_dc")
    op.execute("""CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'ledger_entries is append-only'; END $$""")
