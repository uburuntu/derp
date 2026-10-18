"""thumbnails on message media

Stickers (animated and video ones too), GIFs, videos, video notes and files
keep Telegram's preview picture, so a run sees them as a picture when the file
itself isn't one. Nullable columns only: old code ignores them, and rows
captured before simply have no preview.

Revision ID: 0031
Revises: 0029
Create Date: 2026-10-18 12:03:33.778989
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_media", sa.Column("thumb_file_id", sa.String(), nullable=True))
    op.add_column("message_media", sa.Column("thumb_unique_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_media", "thumb_unique_id")
    op.drop_column("message_media", "thumb_file_id")
