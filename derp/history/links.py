"""Message references as links: "#970746" in an answer opens that message.

Transcripts name messages by id (``#id``), so that is how Derp cites them; notes and scheduled jobs
have their own numbers and are named "note 42" / "job 12", never ``#``. ``message_links`` resolves an
answer's references to stored messages of this chat (``Chat.message_link``) and hands the sender the
rewrite to apply to what goes out: Derp's history keeps the text as written, without URLs. Numbers
that aren't this chat's messages ("#1 pick"), code and existing links are left as written, and chats
without message links (basic groups, private chats) keep plain references.
"""

import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.models import Chat, Message
from derp.telegram.text import map_prose

_REFERENCE = re.compile(r"(?<![\w\[\\/#&!])#(\d{1,12})(?![\w\]])")


def references(markdown: str) -> set[int]:
    found: set[int] = set()

    def collect(text: str) -> str:
        found.update(int(match.group(1)) for match in _REFERENCE.finditer(text))
        return text

    map_prose(markdown, collect)
    return found


def link_references(markdown: str, link: Callable[[int], str | None]) -> str:
    """Each ``#id`` that ``link`` resolves becomes ``[#id](url)``."""

    def replace(match: re.Match[str]) -> str:
        url = link(int(match.group(1)))
        return f"[{match.group(0)}]({url})" if url else match.group(0)

    return map_prose(markdown, lambda text: _REFERENCE.sub(replace, text))


async def message_links(session: AsyncSession, chat: Chat, markdown: str) -> Callable[[str], str] | None:
    """The rewrite linking ``markdown``'s references to this chat's stored messages; None when there are none."""
    wanted = references(markdown) if chat.link_prefix else set[int]()
    if not wanted:
        return None
    stored = set(
        await session.scalars(
            select(Message.message_id).where(
                Message.chat_id == chat.id, Message.message_id.in_(wanted), Message.deleted_at.is_(None)
            )
        )
    )
    if not stored:
        return None
    return lambda text: link_references(
        text, lambda message_id: chat.message_link(message_id) if message_id in stored else None
    )
