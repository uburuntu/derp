"""enforce credit balance invariants

Revision ID: 83675a9a2ab5
Revises: 21630de6de88
Create Date: 2026-07-20 18:38:29.244590+00:00

"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "83675a9a2ab5"
down_revision: str | None = "21630de6de88"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "user_credits_non_negative",
        "users",
        "credits >= 0",
    )
    op.create_check_constraint(
        "chat_credits_non_negative",
        "chats",
        "credits >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chat_credits_non_negative",
        "chats",
        type_="check",
    )
    op.drop_constraint(
        "user_credits_non_negative",
        "users",
        type_="check",
    )
