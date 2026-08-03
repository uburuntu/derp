"""support durable media delivery kinds

Revision ID: 7e1fee80789f
Revises: aaa08fcdc5b5
Create Date: 2026-07-21 00:02:14.135144+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e1fee80789f"
down_revision: str | None = "aaa08fcdc5b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("artifact_kind_allowed", "artifacts", type_="check")
    op.create_check_constraint(
        "artifact_kind_allowed",
        "artifacts",
        "kind::text = ANY (ARRAY["
        "'image'::text, 'video'::text, 'audio'::text, 'voice'::text, "
        "'document'::text])",
    )


def downgrade() -> None:
    op.drop_constraint("artifact_kind_allowed", "artifacts", type_="check")
    op.create_check_constraint(
        "artifact_kind_allowed",
        "artifacts",
        "kind::text = ANY (ARRAY["
        "'image'::text, 'video'::text, 'audio'::text, 'document'::text])",
    )
