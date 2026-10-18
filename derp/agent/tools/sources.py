"""Where a tool's picture or clip comes from: a stored message, a member's profile photo or the chat photo.

A result an earlier tool of the run kept (``from_result``, see ``derp.agent.outputs``)
comes first when named.

Message media is the one named, or else the current message's, the one it
replies to, or the images the run looks at (``RunDeps.seen``: the asker's photo
just before, the image behind an answered run). A profile photo belongs to a
person named in this chat (name or @username, matched against members seen
here; "me" is the asker), else a stored message's author (a forward's original
author when Telegram names them), else the replied-to message's, else the
asker; tools never take a user id.
Failures raise ``Refused``, whose text is meant for the model.
"""

from dataclasses import dataclass

from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.images import Portrait
from derp.agent.outputs import Kept, Refused
from derp.history.models import Message, MessageMedia, User
from derp.history.queries import resolve_members
from derp.history.render import load_views
from derp.telegram.media import MediaUnavailableError, chat_photo_bytes, profile_photo_bytes, sniff_image

VISUAL = ("photo", "sticker", "document", "animation", "video", "video_note")
SOUND = ("voice", "audio", "video", "video_note")


@dataclass(frozen=True, slots=True)
class Source:
    data: bytes
    type: str  # the Telegram media type it came from ("profile_photo" / "chat_photo" for avatars)
    image: bool  # a still picture Pillow can open (GIFs count as clips)
    kept: Kept | None = None  # the run's kept result it is


def kept_source(deps: RunDeps, ref: str, types: tuple[str, ...]) -> Source:
    """A result kept earlier in this run, by the id the tool returned (e.g. "r1")."""
    kept = deps.kept.get(ref.strip().strip('"#').lower())
    if kept is None:
        known = ", ".join(f'"{r}"' for r in deps.kept) or "none"
        raise Refused(f"No kept result {ref!r} in this answer (kept: {known}).")
    if kept.made.sendable not in types:
        raise Refused(f'Result "{kept.ref}" is a {kept.made.sendable}, which this tool can\'t use.')
    kind = sniff_image(kept.made.data)
    return Source(kept.made.data, kept.made.sendable, kind is not None and kind != "image/gif", kept)


async def replied_to(deps: RunDeps) -> int | None:
    async with deps.db.session() as session:
        return await session.scalar(
            select(Message.reply_to_message_id).where(
                Message.chat_id == deps.chat.id, Message.message_id == deps.trigger_message_id
            )
        )


async def message_media(deps: RunDeps, message_id: int | None, types: tuple[str, ...]) -> Source:
    anchors = (
        [message_id]
        if message_id is not None
        else list(dict.fromkeys([deps.trigger_message_id, await replied_to(deps), *deps.seen]))
    )
    async with deps.db.session() as session:
        rows = list(
            await session.scalars(
                select(MessageMedia)
                .join(
                    Message,
                    (Message.chat_id == MessageMedia.chat_id)
                    & (Message.message_id == MessageMedia.message_id),
                )
                .where(
                    MessageMedia.chat_id == deps.chat.id,
                    MessageMedia.message_id.in_([a for a in anchors if a]),
                    MessageMedia.type.in_(types),
                    Message.deleted_at.is_(None),
                )
                .order_by(MessageMedia.idx)
            )
        )
    rows.sort(key=lambda r: anchors.index(r.message_id))
    if not rows:
        where = f"message #{message_id}" if message_id is not None else "the messages this one is about"
        raise Refused(f"No usable media in {where}; name the message with message_id.")
    row = rows[0]
    try:
        data = await deps.gateway.fetch(
            row.file_id, bot_id=row.bot_id, key=row.file_unique_id, size_hint=row.file_size
        )
    except MediaUnavailableError as unavailable:
        raise Refused(f"That file can't be downloaded ({unavailable.reason}).") from None
    kind = sniff_image(data)
    return Source(data, row.type, image=kind is not None and kind != "image/gif")


def _named(user: User) -> Portrait:
    return Portrait(user.id, user.label)


async def _portrait(deps: RunDeps, user_id: int) -> Portrait:
    async with deps.db.session() as session:
        user = await session.get(User, user_id)
    return _named(user) if user is not None else Portrait(user_id)


async def resolve_person(deps: RunDeps, name: str) -> Portrait:
    """The one member of this chat a name or @username points at ("me": the asker)."""
    wanted = name.strip().lstrip("@").casefold()
    if wanted == "me":
        return await _portrait(deps, deps.user_id)
    async with deps.db.session() as session:
        ids = await resolve_members(session, deps.chat.id, [name])
        users = list(await session.scalars(select(User).where(User.id.in_(ids)))) if ids else []
    if not users:
        raise Refused(f"No one called {name!r} here; ask who they mean.")
    if len(users) > 1:  # an exact @username or first name beats partial matches
        exact = [
            u
            for u in users
            if wanted in ((u.username or "").casefold(), u.first_name.casefold(), u.full_name.casefold())
        ]
        users = exact or users
    if len(users) > 1:
        options = ", ".join(_named(u).name for u in users[:8])
        raise Refused(f"{name!r} could be: {options}. Ask which one, or use their @username.")
    return _named(users[0])


async def photo_owner(deps: RunDeps, message_id: int | None, name: str | None = None) -> Portrait:
    """Whose profile photo: the named person, else message_id's author, else the replied-to one's, else the asker.

    A message's author is its sender, or for a forward the person it was forwarded from when Telegram names
    them (a hidden account, a chat or a channel leaves the sender).
    """
    if name:
        return await resolve_person(deps, name)
    anchor = message_id if message_id is not None else await replied_to(deps)
    if anchor is None:
        return await _portrait(deps, deps.user_id)
    async with deps.db.session() as session:
        row = await session.scalar(
            select(Message).where(Message.chat_id == deps.chat.id, Message.message_id == anchor)
        )
        views = await load_views(session, [row]) if row is not None else []
    who = views[0].author if views else None
    if who is not None and who.user_id is not None:
        return Portrait(who.user_id, who.label)
    sender = row.user_id if row is not None else None
    if sender is None and message_id is not None:
        raise Refused(f"Message #{message_id} isn't in this chat's history or has no person behind it.")
    return await _portrait(deps, sender or deps.user_id)


async def avatar_source(deps: RunDeps, message_id: int | None, name: str | None = None) -> Source:
    owner = await photo_owner(deps, message_id, name)
    try:
        data = await profile_photo_bytes(deps.bot, deps.gateway, owner.user_id)
    except MediaUnavailableError as unavailable:
        raise Refused(f"No profile photo to use: {unavailable.reason}.") from None
    return Source(data, "profile_photo", image=sniff_image(data) is not None)


async def chat_photo_source(deps: RunDeps) -> Source:
    try:
        data = await chat_photo_bytes(deps.bot, deps.gateway, deps.chat.id)
    except MediaUnavailableError as unavailable:
        raise Refused(f"No chat photo to use: {unavailable.reason}.") from None
    return Source(data, "chat_photo", image=sniff_image(data) is not None)
