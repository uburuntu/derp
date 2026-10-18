"""Making images: OpenRouter's Images API on the image pin (sent by the tool, through ``outputs``).

Billing is all-or-nothing per image. The provider call is ledgered against the
run only once Telegram accepted the photo (or, for an image kept for a next
step, the result made from it: ``derp.agent.outputs``); if nothing arrives the
cost is absorbed (ledgered without a run) and nothing is charged. Edits use the
images of stored messages in the same chat, members' profile photos or the
chat photo as references. The prompt says whose each profile photo is, in the
order they are sent, so every likeness lands on its own character, and names
the members without a photo so they are drawn from the description.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from aiogram import Bot

from derp.agent.media import inspect, referenced_media
from derp.db.engine import Database
from derp.inference.api import GeneratedImage, OpenRouterAPI, OpenRouterError
from derp.inference.roles import RolePin
from derp.telegram.media import Media, MediaGateway, chat_photo_bytes, profile_photo_bytes

log = logging.getLogger("derp.images")
ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4")


@dataclass(frozen=True, slots=True)
class Portrait:
    """A member whose profile photo is a reference (resolved by name or message, never a model's user id)."""

    user_id: int
    name: str = ""  # who the image model is told it is ("" in jobs stored before names were kept)

    def as_arg(self) -> dict[str, object]:
        return {"id": self.user_id, "name": self.name}

    @classmethod
    def from_arg(cls, arg: object) -> Portrait:
        if isinstance(arg, dict):
            found = cast(dict[str, object], arg)
            return cls(int(cast(int, found["id"])), str(found.get("name") or ""))
        return cls(int(cast(int, arg)))  # a bare id: a job stored before names were kept


@dataclass(frozen=True, slots=True)
class ImageJob:
    prompt: str
    aspect_ratio: str = "1:1"
    references: tuple[int, ...] = ()  # stored message ids (this chat) whose images are edited
    avatar_of: tuple[Portrait, ...] = ()  # whose profile photos are references, in order
    faceless: tuple[str, ...] = ()  # members in the picture without a profile photo
    chat_photo: bool = False  # edit this chat's photo

    def as_args(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "aspect_ratio": self.aspect_ratio,
            "references": list(self.references),
            "avatar_of": [portrait.as_arg() for portrait in self.avatar_of],
            "faceless": list(self.faceless),
            "chat_photo": self.chat_photo,
        }

    @classmethod
    def from_args(cls, args: dict[str, object]) -> ImageJob:
        """A stored job; one from before group portraits (a single ``avatar_of`` id) loads as one portrait."""
        references = cast(list[int], args.get("references") or [])
        avatars = args.get("avatar_of")
        if not isinstance(avatars, list):
            avatars = [] if avatars is None else [avatars]
        return cls(
            prompt=str(args.get("prompt", "")),
            aspect_ratio=str(args.get("aspect_ratio", "1:1")),
            references=tuple(int(r) for r in references),
            avatar_of=tuple(Portrait.from_arg(avatar) for avatar in cast(list[object], avatars)),
            faceless=tuple(str(name) for name in cast(list[object], args.get("faceless") or [])),
            chat_photo=bool(args.get("chat_photo")),
        )

    def cast_note(self, first: int) -> str:
        """Who is who for the image model, the profile photos starting at reference ``first`` (1-based)."""
        notes: list[str] = []
        if self.avatar_of:
            shown = ", ".join(
                f"reference image {first + i} is {portrait.name or 'the person'}"
                for i, portrait in enumerate(self.avatar_of)
            )
            notes.append(f"People: {shown} (profile photos). Keep each likeness on that person's character.")
        if self.faceless:
            notes.append(f"No photo of {', '.join(self.faceless)}: draw them from the description.")
        return " ".join(notes)


async def _references(db: Database, gateway: MediaGateway, chat_id: int, ids: Sequence[int]) -> list[Media]:
    async with db.session() as session:
        found = [
            row for message_id in ids for row in await referenced_media(session, chat_id, message_id, None)
        ]
    rows = list({(row.message_id, row.idx): row for row in found}.values())  # an album's photos come once
    return [item.image for item in await inspect(gateway, rows)]


async def _generate(
    api: OpenRouterAPI, pin: RolePin, prompt: str, aspect_ratio: str, references: list[Media]
) -> GeneratedImage:
    error: OpenRouterError | None = None
    for model in (pin.model, *pin.fallbacks):
        try:
            return await api.generate_image(
                model,
                prompt,
                only=pin.only.get(model, ()),
                zdr=pin.zdr,
                references=[(image.data, image.media_type) for image in references],
                aspect_ratio=aspect_ratio,
            )
        except OpenRouterError as failed:  # next model in the pin
            log.info("image model failed: %s", failed.status)
            error = failed
    raise error or OpenRouterError(502, "no image model")


async def draw(
    *,
    db: Database,
    api: OpenRouterAPI,
    bot: Bot,
    gateway: MediaGateway,
    pin: RolePin,
    chat_id: int,
    job: ImageJob,
    extra: Sequence[bytes] = (),
) -> GeneratedImage:
    """Generate one image (not yet ledgered: the caller ledgers it once it knows whether it was delivered).

    ``extra`` adds reference pictures by content (a result an earlier tool of the run kept). Every
    reference reaches the model normalized (``MediaGateway.image`` / ``normalize``), in this order:
    stored messages' images, ``extra``, the profile photos (named in the prompt), the chat photo.
    """
    references = await _references(db, gateway, chat_id, job.references)
    first_avatar = len(references) + len(extra) + 1
    pictures = [
        *extra,
        *[await profile_photo_bytes(bot, gateway, portrait.user_id) for portrait in job.avatar_of],
        *([await chat_photo_bytes(bot, gateway, chat_id)] if job.chat_photo else []),
    ]
    references += [await gateway.normalize(data) for data in pictures]
    prompt = "\n\n".join(part for part in (job.prompt, job.cast_note(first_avatar)) if part)
    return await _generate(api, pin, prompt, job.aspect_ratio, references)


def filename(image: GeneratedImage) -> str:
    return "derp.jpg" if "jpeg" in image.media_type else "derp.png"
