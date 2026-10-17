"""Making images: OpenRouter's Images API on the image pin, then a photo reply.

Billing is all-or-nothing per image. The provider call is ledgered against the
run only once Telegram accepted the photo; if sending fails the cost is
absorbed (ledgered without a run) and nothing is charged. Edits use the
images of stored messages in the same chat, a member's profile photo or the
chat photo as references.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message

from derp.agent.media import inspect, referenced_media
from derp.db.engine import Database
from derp.history.capture import record_outgoing
from derp.inference.api import GeneratedImage, OpenRouterAPI, OpenRouterError
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.roles import ModelRole, RolePin
from derp.telegram.media import MediaGateway, chat_photo_bytes, profile_photo_bytes, sniff_image
from derp.telegram.sender import Target

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


async def _references(
    db: Database, gateway: MediaGateway, chat_id: int, ids: Sequence[int]
) -> list[tuple[bytes, str]]:
    async with db.session() as session:
        rows = [
            row for message_id in ids for row in await referenced_media(session, chat_id, message_id, None)
        ]
    return [(item.image.data, item.image.media_type) for item in await inspect(gateway, rows)]


async def _generate(
    api: OpenRouterAPI, pin: RolePin, job: ImageJob, references: list[tuple[bytes, str]]
) -> GeneratedImage:
    error: OpenRouterError | None = None
    for model in (pin.model, *pin.fallbacks):
        try:
            return await api.generate_image(
                model,
                job.prompt,
                only=pin.only.get(model, ()),
                zdr=pin.zdr,
                references=references,
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
) -> GeneratedImage:
    """Generate one image (not yet ledgered: the caller ledgers it once it knows whether it was delivered)."""
    references = await _references(db, gateway, chat_id, job.references)
    photos = [
        *([await profile_photo_bytes(bot, gateway, job.avatar_of)] if job.avatar_of is not None else []),
        *([await chat_photo_bytes(bot, gateway, chat_id)] if job.chat_photo else []),
    ]
    references += [(data, sniff_image(data) or "image/jpeg") for data in photos]
    return await _generate(api, pin, job, references)


def filename(image: GeneratedImage) -> str:
    return "derp.jpg" if "jpeg" in image.media_type else "derp.png"


async def make_image(
    *,
    db: Database,
    api: OpenRouterAPI,
    bot: Bot,
    gateway: MediaGateway,
    pin: RolePin,
    target: Target,
    job: ImageJob,
    run_id: int,
    funding: str,
    user_id: int,
) -> Message:
    """Generate, send and ledger one image (approved actions); raises when nothing reached the chat."""
    image = await draw(db=db, api=api, bot=bot, gateway=gateway, pin=pin, chat_id=target.chat_id, job=job)
    try:
        sent = await bot.send_photo(
            chat_id=target.chat_id,
            message_thread_id=target.thread_id,
            photo=BufferedInputFile(image.data, filename=filename(image)),
            reply_parameters=target.reply_parameters,
        )
    except BaseException:
        await ledger_image(db, image, target.chat_id, user_id, funding="creator", run_id=None)  # absorbed
        raise
    await ledger_image(db, image, target.chat_id, user_id, funding=funding, run_id=run_id)
    async with db.transaction() as session:
        await record_outgoing(session, [sent], bot.id)
    return sent


async def ledger_image(
    db: Database, image: GeneratedImage, chat_id: int, user_id: int, *, funding: str, run_id: int | None
) -> None:
    """Ledger the provider call: against the run once delivered, else as ``creator`` without a run."""
    async with db.transaction() as session:
        record_usage(
            session,
            simple_usage(image.model, image.cost_usd),
            kind="image",
            funding=funding,
            role=ModelRole.IMAGE.value,
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            units=1,
        )
