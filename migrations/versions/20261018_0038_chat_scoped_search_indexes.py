"""chat-scoped search indexes

History search always filters by chat, but the full-text and trigram GIN
indexes spanned every chat, so a common word read other chats' postings and
rows before the chat filter. Both now lead with ``chat_id`` (``btree_gin``, a
trusted extension). The new indexes are built ``CONCURRENTLY`` before the old
ones are dropped, outside a transaction (an autocommit block), so writes to
``messages`` never wait on the build and search always has an index. A build
that failed leaves an invalid index behind: it is dropped first on a rerun.
Expand-only: old code runs the same chat-filtered queries.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-18 13:48:11.964991
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHAT_SCOPED = {
    "ix_messages_chat_tsv": "using gin (chat_id, tsv)",
    "ix_messages_chat_text_trgm": "using gin (chat_id, text gin_trgm_ops)",
}
ALL_CHATS = {
    "ix_messages_tsv": "using gin (tsv)",
    "ix_messages_text_trgm": "using gin (text gin_trgm_ops)",
}


def _swap(build: dict[str, str], drop: dict[str, str]) -> None:
    with op.get_context().autocommit_block():
        for name, how in build.items():
            op.execute(f"drop index concurrently if exists {name}")
            op.execute(f"create index concurrently {name} on messages {how}")
        for name in drop:
            op.execute(f"drop index concurrently if exists {name}")


def upgrade() -> None:
    op.execute("create extension if not exists btree_gin")
    _swap(CHAT_SCOPED, ALL_CHATS)


def downgrade() -> None:
    _swap(ALL_CHATS, CHAT_SCOPED)
