"""plain sticker emoji and settled transcripts

Sticker emoji are now indexed plain (no U+FE0F presentation selector, no skin
tone), the way ``send_sticker`` and ``react`` compare them, so rows indexed
before are rewritten the same way. A recording that won't get a transcript
(too long, or the last attempt failed) now stores an empty transcript with the
reason in ``transcript_model``, like an image whose note came back empty;
rows marked before get the empty transcript. Data only: old code reads both
(a plain emoji still matches its comparison; an empty transcript renders as a
bare recording).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-17 22:22:46.827074
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # translate() deletes U+FE0F (65039) and the skin-tone modifiers U+1F3FB..U+1F3FF (127995..127999).
    op.execute(
        "update stickers set emoji = translate(emoji, chr(65039) || chr(127995) || chr(127996) || chr(127997)"
        " || chr(127998) || chr(127999), '') where emoji is not null"
    )
    op.execute(
        "update message_media set transcript = ''"
        " where transcript is null and transcript_model in ('too-long', 'failed')"
    )


def downgrade() -> None:
    # Plain sticker emoji stay plain: old code strips the selector itself and matches them as they are.
    op.execute(
        "update message_media set transcript = null"
        " where transcript = '' and transcript_model in ('too-long', 'failed')"
    )
