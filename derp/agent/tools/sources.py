"""Where a tool's picture or clip comes from: a stored message, a kept result, a profile photo or the chat photo.

Tools name it with one ``source`` string (``source_of``): "#123" for a stored message's media — or, for a
message without a picture, its author's profile photo ("meme him" in reply to his message) — "r1" for a
result an earlier tool of the run kept (see ``derp.agent.outputs``), "chat" for the chat photo, or a
member's name, @username or "me" (the asker) for their profile photo; nothing for the media the message
is about — the current message's, the one it replies to, or the images the run looks at
(``RunDeps.seen``: the asker's photo just before, the image behind an answered run). A message's author
is a forward's original author when Telegram names them (``history.render.author``), else its sender; a
name is matched against members seen in this chat, else against the named authors of recent forwards
here (transcripts show them as "fwd from Mike"). Tools never take a user id: people are only ever reached
through this chat's members and messages. Failures raise ``Refused``, whose text is meant for the model.
"""

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field
from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.images import Portrait
from derp.agent.outputs import Kept, Refused
from derp.history.models import Message, MessageMedia, User
from derp.history.queries import resolve_members
from derp.history.render import load_views, origin_author
from derp.telegram.media import MediaUnavailableError, chat_photo_bytes, profile_photo_bytes, sniff_image

VISUAL = ("photo", "sticker", "document", "animation", "video", "video_note")
SOUND = ("voice", "audio", "video", "video_note")
SOURCE = (
    '"#id": that message\'s media (none: its author\'s profile photo); "r1": a result kept earlier in this '
    'answer; a name, @username or "me": their profile photo; "chat": the chat photo. Default: the media this '
    "message is about."
)
type SourceArg = Annotated[str | None, Field(description=SOURCE)]  # a tool's ``source`` argument
# Chains: a media tool's result kept unposted ("r1") is the next tool's source, posted only if nothing uses it.
type Final = Annotated[
    bool, Field(description="False keeps the result unposted for a next step, as its source.")
]
MESSAGE = re.compile(r"#?(\d+)")
KEPT = re.compile(r"r\d+", re.IGNORECASE)
CHAT_PHOTO = frozenset({"chat", "chat photo", "the chat"})
THIS = frozenset({"", "this"})  # the media the message is about
FORWARDS_SCANNED = 200  # recent forwards searched for a named author who isn't a member


@dataclass(frozen=True, slots=True)
class Source:
    data: bytes
    type: str  # the Telegram media type it came from ("profile_photo" / "chat_photo" for avatars)
    image: bool  # a still picture Pillow can open (GIFs count as clips)
    kept: Kept | None = None  # the run's kept result it is
    owner: str = ""  # whose profile photo it is


def kept_ref(source: str | None) -> str | None:
    """The kept result a source names ("r1"), else None."""
    ref = (source or "").strip().strip('"#').lower()
    return ref if KEPT.fullmatch(ref) else None


async def source_of(deps: RunDeps, source: str | None, types: tuple[str, ...] = VISUAL) -> Source:
    """The picture or clip a tool's ``source`` names (see the module docstring)."""
    text = (source or "").strip()
    if ref := kept_ref(text):
        return kept_source(deps, ref, types)
    if text.lower() in THIS:
        return await message_media(deps, None, types)
    if found := MESSAGE.fullmatch(text):
        return await message_media(deps, int(found.group(1)), types)
    if text.lower() in CHAT_PHOTO:
        return await chat_photo_source(deps)
    return await avatar_source(deps, text)


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
    """The media of a named message, else of the messages this one is about; a named message without any is
    its author's profile photo where a picture will do."""
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
    if not rows and message_id is not None and "photo" in types:
        return await photo_of(deps, await author_of(deps, message_id))
    if not rows:
        raise Refused('No usable media in the messages this one is about; name one as source, e.g. "#123".')
    row = rows[0]
    try:
        data = await deps.gateway.fetch(
            row.file_id, bot_id=row.bot_id, key=row.file_unique_id, size_hint=row.file_size
        )
    except MediaUnavailableError as unavailable:
        raise Refused(f"That file can't be downloaded ({unavailable.reason}).") from None
    kind = sniff_image(data)
    return Source(data, row.type, image=kind is not None and kind != "image/gif")


async def author_of(deps: RunDeps, message_id: int) -> Portrait:
    """Who wrote a stored message of this chat: a forward's named original author, else its sender."""
    async with deps.db.session() as session:
        row = await session.get(Message, (deps.chat.id, message_id))
        views = await load_views(session, [row]) if row is not None and row.deleted_at is None else []
    who = views[0].author if views else None
    if who is not None and who.user_id is not None:
        return Portrait(who.user_id, who.label)
    if views and views[0].row.user_id is not None:  # a hidden account's forward: whoever forwarded it
        return await _portrait(deps, views[0].row.user_id)
    raise Refused(f"Message #{message_id} isn't in this chat's history or has no person behind it.")


def _named(user: User) -> Portrait:
    return Portrait(user.id, user.label)


async def _portrait(deps: RunDeps, user_id: int) -> Portrait:
    async with deps.db.session() as session:
        user = await session.get(User, user_id)
    return _named(user) if user is not None else Portrait(user_id)


async def resolve_person(deps: RunDeps, name: str) -> Portrait:
    """The one person a name or @username points at: a member of this chat ("me": the asker), else the named
    author of a recent forward here."""
    wanted = name.strip().lstrip("@").casefold()
    if wanted == "me":
        return await _portrait(deps, deps.user_id)
    async with deps.db.session() as session:
        ids = await resolve_members(session, deps.chat.id, [name])
        users = list(await session.scalars(select(User).where(User.id.in_(ids)))) if ids else []
    if not users:
        if (forwarded := await _forwarded(deps, wanted)) is not None:
            return forwarded
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


async def _forwarded(deps: RunDeps, wanted: str) -> Portrait | None:
    """The named author of a recent forward in this chat who goes by ``wanted`` (casefolded)."""
    async with deps.db.session() as session:
        origins = await session.scalars(
            select(Message.forward_origin)
            .where(
                Message.chat_id == deps.chat.id,
                Message.forward_origin.is_not(None),
                Message.deleted_at.is_(None),
            )
            .order_by(Message.message_id.desc())
            .limit(FORWARDS_SCANNED)
        )
        for origin in origins:
            who = origin_author(origin)
            names = (who.name, who.first_name or "", who.username or "") if who else ()
            if who is not None and who.user_id is not None and wanted in (n.casefold() for n in names):
                return Portrait(who.user_id, who.label)
    return None


async def avatar_source(deps: RunDeps, name: str) -> Source:
    return await photo_of(deps, await resolve_person(deps, name))


async def photo_of(deps: RunDeps, owner: Portrait) -> Source:
    """``owner``'s profile photo."""
    try:
        data = await profile_photo_bytes(deps.bot, deps.gateway, owner.user_id)
    except MediaUnavailableError as unavailable:
        raise Refused(f"No profile photo of {owner.name or 'them'} to use: {unavailable.reason}.") from None
    return Source(data, "profile_photo", image=sniff_image(data) is not None, owner=owner.name)


async def chat_photo_source(deps: RunDeps) -> Source:
    try:
        data = await chat_photo_bytes(deps.bot, deps.gateway, deps.chat.id)
    except MediaUnavailableError as unavailable:
        raise Refused(f"No chat photo to use: {unavailable.reason}.") from None
    return Source(data, "chat_photo", image=sniff_image(data) is not None)
