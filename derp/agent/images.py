"""Making images: OpenRouter's Images API on the image pin (sent by the tool, through ``outputs``).

Billing is all-or-nothing per image. The provider call is ledgered against the
run only once Telegram accepted the photo (or, for an image kept for a next
step, the result made from it: ``derp.agent.outputs``); if nothing arrives the
cost is absorbed (ledgered without a run) and nothing is charged. Edits use the
images of stored messages in the same chat, a member's profile photo or the
chat photo as references.
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
class ImageJob:
    prompt: str
    aspect_ratio: str = "1:1"
    references: tuple[int, ...] = ()  # stored message ids (this chat) whose images are edited
    avatar_of: int | None = None  # whose profile photo is edited (resolved from a message, never model input)
    chat_photo: bool = False  # edit this chat's photo

    def as_args(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "aspect_ratio": self.aspect_ratio,
            "references": list(self.references),
            "avatar_of": self.avatar_of,
            "chat_photo": self.chat_photo,
        }

    @classmethod
    def from_args(cls, args: dict[str, object]) -> ImageJob:
        references = cast(list[int], args.get("references") or [])
        return cls(
            prompt=str(args.get("prompt", "")),
            aspect_ratio=str(args.get("aspect_ratio", "1:1")),
            references=tuple(int(r) for r in references),
            avatar_of=int(cast(int, args["avatar_of"])) if args.get("avatar_of") is not None else None,
            chat_photo=bool(args.get("chat_photo")),
        )


async def _references(db: Database, gateway: MediaGateway, chat_id: int, ids: Sequence[int]) -> list[Media]:
    async with db.session() as session:
        found = [
            row for message_id in ids for row in await referenced_media(session, chat_id, message_id, None)
        ]
    rows = list({(row.message_id, row.idx): row for row in found}.values())  # an album's photos come once
    return [item.image for item in await inspect(gateway, rows)]


async def _generate(
    api: OpenRouterAPI, pin: RolePin, job: ImageJob, references: list[Media]
) -> GeneratedImage:
    error: OpenRouterError | None = None
    for model in (pin.model, *pin.fallbacks):
        try:
            return await api.generate_image(
                model,
                job.prompt,
                only=pin.only.get(model, ()),
                zdr=pin.zdr,
                references=[(image.data, image.media_type) for image in references],
                aspect_ratio=job.aspect_ratio,
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
    reference reaches the model normalized (``MediaGateway.image`` / ``normalize``).
    """
    references = await _references(db, gateway, chat_id, job.references)
    pictures = [
        *extra,
        *([await profile_photo_bytes(bot, gateway, job.avatar_of)] if job.avatar_of is not None else []),
        *([await chat_photo_bytes(bot, gateway, chat_id)] if job.chat_photo else []),
    ]
    references += [await gateway.normalize(data) for data in pictures]
    return await _generate(api, pin, job, references)


def filename(image: GeneratedImage) -> str:
    return "derp.jpg" if "jpeg" in image.media_type else "derp.png"
