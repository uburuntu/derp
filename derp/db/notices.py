"""Atomic claims for versioned one-time user notices."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.models import UserNotice


async def claim_user_notice(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notice: str,
    version: int,
) -> bool:
    """Return true only for the caller that advances a notice version."""
    if not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")
    normalized = notice.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("notice must contain at most 64 characters")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("version must be a positive integer")
    statement = insert(UserNotice).values(
        user_id=user_id,
        notice=normalized,
        version=version,
        shown_at=datetime.now(UTC),
    )
    claimed = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[UserNotice.user_id, UserNotice.notice],
            set_={"version": version, "shown_at": datetime.now(UTC)},
            where=UserNotice.version < version,
        ).returning(UserNotice.version)
    )
    return claimed is not None


__all__ = ["claim_user_notice"]
