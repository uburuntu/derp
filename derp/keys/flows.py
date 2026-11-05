"""Plugging a key in, choosing its cap and model, turning it on in chats, and the owner's key page.

Plugging in happens in private only. A group's /settings links here (``start=key-<chat ref>``, the chat
id signed so a link can't be made for a chat it wasn't shown in); /settings in private and the key page
start it without a group. The person pastes the key (a message ``history`` never stores; deleted at once),
then picks a monthly cap and a model; a key meant for a group is then offered there — turned on at once
when they are an admin of it, else as a card an admin turns on (``Turn on`` / ``Not now``). A key pasted
into a group is deleted (when Derp may) with a warning to revoke it.

In a chat, the key's owner and the chat's admins may pause it or unplug it there (the refusal notice's
buttons and the /settings keys page); only admins turn one on. In private, the owner's key page changes
its model and cap, pauses or resumes it, unplugs it from a chat, or removes it everywhere. Every press
re-checks who may make it.
"""

import base64
import contextlib
import hashlib
import hmac
import logging
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.history.render import one_line
from derp.inference.api import OpenRouterAPI, OpenRouterError, OpenRouterManagement
from derp.keys.models import ContributedKey, KeyChat, KeyRequest
from derp.keys.notices import KeyAction, brand, model_label, owner_name, when
from derp.keys.pasted import pasted_key
from derp.keys.register import Refused, allow_models, plug_in, take_out
from derp.keys.routing import (
    Prices,
    candidates,
    cycle_start,
    next_cycle,
    offered_models,
    pause,
    prices,
    resume,
    spent,
)
from derp.locales import resolve, t
from derp.memory.notes import is_admin
from derp.money.free import offer_continues
from derp.money.funding import Funder
from derp.telegram.bot import bot_username
from derp.telegram.buttons import Press, button, link, markup, on_press
from derp.telegram.sender import Target, send_markdown
from derp.telegram.text import literal

log = logging.getLogger("derp.keys")
COMPANIES = {"openai": "OpenAI", "anthropic": "Anthropic"}
AMOUNT = re.compile(r"^\s*\$?\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*\$?\s*$")


# Plug-in links: the chat id, signed, so a link can't be forged for a chat it wasn't shown in.


def _mac(settings: Settings, raw: bytes) -> bytes:
    secret = (settings.keys_secret or settings.telegram_bot_token).encode()
    return hmac.new(secret, b"key-link:" + raw, hashlib.sha256).digest()[:6]


def chat_ref(settings: Settings, chat_id: int) -> str:
    raw = struct.pack(">q", chat_id)
    return base64.urlsafe_b64encode(raw + _mac(settings, raw)).decode().rstrip("=")


def chat_from_ref(settings: Settings, ref: str) -> int | None:
    try:
        data = base64.urlsafe_b64decode(ref + "=" * (-len(ref) % 4))
    except ValueError:
        return None
    raw, mac = data[:8], data[8:]
    if len(raw) != 8 or not hmac.compare_digest(mac, _mac(settings, raw)):
        return None
    return struct.unpack(">q", raw)[0]


async def plug_link(bot: Bot, settings: Settings, chat_id: int) -> str:
    return f"https://t.me/{await bot_username(bot)}?start=key-{chat_ref(settings, chat_id)}"


# Shared pieces.


@dataclass(frozen=True, slots=True)
class Plugged:
    """A key with its owner, as chats name it."""

    key: ContributedKey
    owner: User | None


def per_reply(funder: Funder, priced: Prices | None) -> str:
    """A model's price for a typical reply, uncached, as the picker shows it."""
    if priced is None:
        return "?"
    usd = priced.turn(*funder.turn_tokens())
    return f"{usd:.3f}" if usd < Decimal("0.1") else f"{usd:.2f}"


async def _request(session: AsyncSession, settings: Settings, user_id: int) -> KeyRequest | None:
    """The person's step in progress, if it hasn't expired."""
    row = await session.get(KeyRequest, user_id)
    if row is None or row.at < datetime.now(UTC) - timedelta(minutes=settings.keys.request_minutes):
        return None
    return row


async def _set_request(
    session: AsyncSession, user_id: int, step: str, *, chat_id: int | None = None, key_id: int | None = None
) -> None:
    await session.execute(delete(KeyRequest).where(KeyRequest.user_id == user_id))
    session.add(KeyRequest(user_id=user_id, step=step, chat_id=chat_id, key_id=key_id, at=datetime.now(UTC)))


async def _own_key(session: AsyncSession, user_id: int, key_id: int) -> ContributedKey | None:
    key = await session.get(ContributedKey, key_id)
    return key if key is not None and key.user_id == user_id and key.removed_at is None else None


async def live_keys(session: AsyncSession, user_id: int) -> list[ContributedKey]:
    statement = select(ContributedKey).where(
        ContributedKey.user_id == user_id, ContributedKey.removed_at.is_(None)
    )
    return list(await session.scalars(statement.order_by(ContributedKey.id)))


# Plugging in (private).


async def open_plug(bot: Bot, person: TgUser, db: Database, settings: Settings, chat_id: int | None) -> None:
    """The plug-in intro in their private chat, waiting for the pasted key; a link from a group names it, and
    offers keys they already plugged in."""
    user_id, locale = person.id, resolve(person.language_code)
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id) if chat_id else None
        await _set_request(session, user_id, "paste", chat_id=chat.id if chat else None)
        keys = await live_keys(session, user_id)
    title = t(locale, "keys.for_chat", title=literal(one_line(chat.title or "", 60))) if chat else ""
    reuse = [
        button(
            t(locale, "keys.use_button", brand=brand(k.provider), label=literal(k.label)),
            KeyAction(action="use", key=k.id, value=chat_ref(settings, chat.id)),  # signed, like the link
        )
        for k in keys
        if chat is not None and k.dead_at is None
    ]
    text = t(locale, "keys.intro", for_chat=title)
    if reuse:
        text += "\n\n" + t(locale, "keys.or_reuse")
    await send_markdown(bot, Target(user_id), text, reply_markup=markup(*reuse))


class PastedKey(Filter):
    """A message carrying a provider API key (never stored: ``history.middleware``)."""

    async def __call__(self, message: Message) -> bool | dict[str, str]:
        found = pasted_key(message.text or message.caption)
        return {"pasted": found} if found else False


async def on_pasted(
    message: Message,
    pasted: str,
    db: Database,
    settings: Settings,
    bot: Bot,
    funder: Funder,
    api: OpenRouterAPI,
    management: OpenRouterManagement | None,
) -> None:
    """A pasted key: deleted at once; in private it is plugged in, in a group only warned about."""
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    deleted = False
    with contextlib.suppress(TelegramAPIError):
        deleted = await bot.delete_message(message.chat.id, message.message_id)
    if message.chat.type != "private":
        text = t(locale, "keys.pasted_in_group" if deleted else "keys.pasted_in_group_kept")
        plug = link(t(locale, "keys.plug_privately"), await plug_link(bot, settings, message.chat.id))
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(
                bot,
                Target(message.chat.id, message.message_thread_id if message.is_topic_message else None),
                text,
                reply_markup=markup(plug),
            )
        return
    target = Target(message.chat.id)
    if management is None or not settings.keys_secret:
        await send_markdown(bot, target, t(locale, "keys.not_configured"))
        return
    try:
        result = await plug_in(db, settings, management, api, message.from_user.id, pasted)
    except OpenRouterError as error:
        log.info("plugging in failed: %s", error.status)
        result = Refused("unavailable")
    if isinstance(result, Refused):
        await send_markdown(bot, target, t(locale, f"keys.{result.why}"))
        return
    async with db.transaction() as session:
        request = await _request(session, settings, message.from_user.id)
        await _set_request(
            session,
            message.from_user.id,
            "cap",
            chat_id=request.chat_id if request else None,
            key_id=result.id,
        )
    await send_markdown(
        bot,
        target,
        t(locale, "keys.plugged", brand=brand(result.provider), label=literal(result.label)),
        reply_markup=_cap_buttons(settings, locale, result.id),
    )


def _cap_buttons(settings: Settings, locale: str, key_id: int) -> InlineKeyboardMarkup | None:
    caps = [
        button(f"${cap}", KeyAction(action="cap", key=key_id, value=str(cap)))
        for cap in settings.keys.caps_usd
    ]
    other = button(t(locale, "keys.cap_other_button"), KeyAction(action="cap", key=key_id, value="other"))
    return markup(caps[:3], caps[3:], other)


class CapAmount(Filter):
    """A dollar amount sent in private while the person is choosing a custom monthly cap."""

    async def __call__(self, message: Message, db: Database, settings: Settings) -> bool | dict[str, object]:
        if (
            message.chat.type != "private"
            or message.from_user is None
            or not (found := AMOUNT.match(message.text or ""))
        ):
            return False
        async with db.session() as session:
            request = await _request(session, settings, message.from_user.id)
        if request is None or request.step != "cap" or request.key_id is None:
            return False
        try:
            return {"amount": Decimal(found.group(1).replace(",", ".")), "request": request}
        except InvalidOperation:
            return False


async def on_cap_amount(
    message: Message,
    amount: Decimal,
    request: KeyRequest,
    db: Database,
    settings: Settings,
    bot: Bot,
    funder: Funder,
) -> None:
    locale = resolve(message.from_user.language_code if message.from_user else None)
    if not 1 <= amount <= settings.keys.max_cap_usd:
        await send_markdown(
            bot, Target(message.chat.id), t(locale, "keys.cap_bad", max=settings.keys.max_cap_usd)
        )
        return
    await _set_cap(
        bot,
        db,
        settings,
        funder,
        Target(message.chat.id),
        request.user_id,
        request.key_id or 0,
        amount,
        locale,
        edit=None,
    )


async def _set_cap(
    bot: Bot,
    db: Database,
    settings: Settings,
    funder: Funder,
    target: Target,
    user_id: int,
    key_id: int,
    amount: Decimal,
    locale: str,
    *,
    edit: Press | None,
) -> None:
    async with db.transaction() as session:
        key = await _own_key(session, user_id, key_id)
        if key is None:
            return
        first = (
            request := await _request(session, settings, user_id)
        ) is not None and request.key_id == key_id
        key.cap_usd = amount
        if key.paused == "cap":
            await resume(session, key)  # a higher cap: serving again (re-checked on the next ask)
        if first and request is not None:
            request.step = "model"
        text, keyboard = (
            await _model_page(session, settings, funder, key, locale)
            if first
            else await key_page(session, settings, funder, key, locale)
        )
    if edit is not None:
        await edit.edit(text, keyboard)
    else:
        await send_markdown(bot, target, text, reply_markup=keyboard)


async def _model_page(
    session: AsyncSession, settings: Settings, funder: Funder, key: ContributedKey, locale: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    rows: list[InlineKeyboardButton] = []
    for index, model in enumerate(await offered_models(session, settings, key.provider)):
        priced = await prices(session, settings, model.model)
        label = t(
            locale,
            "keys.model_button",
            label=model.label,
            price=per_reply(funder, priced),
            star=" ⭐" if model.recommended else "",
        )
        rows.append(button(label, KeyAction(action="model", key=key.id, value=str(index))))
    return t(locale, "keys.pick_model", cap=f"{key.cap_usd:g}"), markup(*rows)


async def _set_model(
    press: Press,
    db: Database,
    settings: Settings,
    funder: Funder,
    management: OpenRouterManagement | None,
    key_id: int,
    index: str,
) -> None:
    async with db.transaction() as session:
        key = await _own_key(session, press.user_id, key_id)
        if key is None:
            return await press.deny("keys.gone")
        models = await offered_models(session, settings, key.provider)
        if not index.isdigit() or int(index) >= len(models):
            return await press.deny("keys.gone")
        chosen = models[int(index)].model
        changed = key.model != chosen
        key.model = chosen
        request = await _request(session, settings, press.user_id)
        first = request is not None and request.key_id == key_id and request.step == "model"
        chat_id = request.chat_id if first and request is not None else None
        if first:
            await session.delete(request)
    if changed and management is not None:  # the curated list may have grown since the key was registered
        with contextlib.suppress(OpenRouterError):
            await allow_models(settings, management, key)
    if changed and not first:
        await _announce_model(press.bot, db, settings, key)
    if not first:
        async with db.session() as session:
            fresh = await session.get(ContributedKey, key_id)
            if fresh is not None:
                text, keyboard = await key_page(session, settings, funder, fresh, press.locale)
                await press.edit(text, keyboard)
        return None
    if chat_id is None:
        return await press.edit(t(press.locale, "keys.ready", model=model_label(settings, chosen)))
    await offer(press, db, settings, key, chat_id)


async def _announce_model(bot: Bot, db: Database, settings: Settings, key: ContributedKey) -> None:
    """A quiet line in each group the key serves: same key, same retention, a different model."""
    async with db.session() as session:
        chats = list(
            await session.scalars(
                select(Chat)
                .join(KeyChat, KeyChat.chat_id == Chat.id)
                .where(KeyChat.key_id == key.id, KeyChat.state == "on", Chat.type != "private")
            )
        )
        owner = await session.get(User, key.user_id)
    for chat in chats:
        public = chat.locale or "en"
        text = t(
            public,
            "keys.model_changed",
            who=owner_name(owner, public),
            brand=brand(key.provider),
            model=model_label(settings, key.model),
        )
        with contextlib.suppress(TelegramAPIError):
            await send_markdown(bot, Target(chat.id), text, silent=True)


async def offer(press: Press, db: Database, settings: Settings, key: ContributedKey, chat_id: int) -> None:
    """Offer a key to a group: on at once for one of its admins, else a card an admin turns on."""
    bot = press.bot
    async with db.transaction() as session:
        chat = await session.get(Chat, chat_id)
        owner = await session.get(User, key.user_id)
        if chat is None or chat.type == "private":
            return await press.edit(t(press.locale, "keys.gone_chat"))
        link_row = await session.get(KeyChat, (key.id, chat_id))
        if link_row is not None and link_row.state == "on":
            return await press.edit(
                t(press.locale, "keys.already_on", title=literal(one_line(chat.title or "", 60)))
            )
    admin = await is_admin(bot, chat, press.user_id)
    public = chat.locale or press.locale
    if admin:
        sent = await _enable(bot, db, settings, chat, key, owner, press.user_id, public)
        text = t(press.locale, "keys.on_in", title=literal(one_line(chat.title or "", 60)))
    else:
        card = t(
            public,
            "keys.card",
            who=owner_name(owner, public),
            brand=brand(key.provider),
            model=model_label(settings, key.model),
            company=COMPANIES[key.provider],
        )
        buttons = markup(
            [
                button(t(public, "keys.turn_on"), KeyAction(action="here_on", key=key.id)),
                button(t(public, "keys.not_now"), KeyAction(action="here_off", key=key.id)),
            ]
        )
        try:
            [*_, sent] = await send_markdown(bot, Target(chat.id), card, reply_markup=buttons)
        except TelegramAPIError:
            return await press.edit(t(press.locale, "keys.card_failed"))
        async with db.transaction() as session:
            row = await session.get(KeyChat, (key.id, chat.id), with_for_update=True)
            if row is None:
                session.add(
                    KeyChat(key_id=key.id, chat_id=chat.id, state="offered", card_message_id=sent.message_id)
                )
            else:
                row.state, row.card_message_id = "offered", sent.message_id
        text = t(press.locale, "keys.asked_admins", title=literal(one_line(chat.title or "", 60)))
    url = chat.message_link(sent.message_id) if sent is not None else None
    await press.edit(text, markup(link(t(press.locale, "keys.open_chat"), url) if url else None))


async def _enable(
    bot: Bot,
    db: Database,
    settings: Settings,
    chat: Chat,
    key: ContributedKey,
    owner: User | None,
    admin_id: int,
    locale: str,
    *,
    card: Press | None = None,
) -> Message | None:
    """Turn a key on in a group: the public notice (the card becomes it), then Continue for waiting asks."""
    now = datetime.now(UTC)
    async with db.transaction() as session:
        row = await session.get(KeyChat, (key.id, chat.id), with_for_update=True)
        if row is None:
            row = KeyChat(key_id=key.id, chat_id=chat.id)
            session.add(row)
        if row.state == "on":
            return None
        row.state, row.enabled_by, row.enabled_at, row.off_noticed_at = "on", admin_id, now, None
        row.paused_by = None
    text = t(
        locale,
        "keys.enabled",
        who=owner_name(owner, locale),
        brand=brand(key.provider),
        model=model_label(settings, key.model),
        company=COMPANIES[key.provider],
    )
    sent: Message | None = None
    if card is not None:
        await card.edit(text)
    else:
        with contextlib.suppress(TelegramAPIError):
            [*_, sent] = await send_markdown(bot, Target(chat.id), text)
    await offer_continues(db, bot, settings, chat.id, locale)
    return sent


# Presses.


async def _chat_press(press: Press, data: KeyAction, db: Database, settings: Settings) -> None:
    """here_on (admins) / here_off / here_pause (the key's owner or an admin), in the chat pressed in."""
    bot = press.bot
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
        key = await session.get(ContributedKey, data.key)
        owner = await session.get(User, key.user_id) if key else None
    if chat is None or key is None or key.removed_at is not None:
        await press.strip()
        return await press.deny("keys.gone")
    if chat.type == "private":  # a private chat's key is its owner's own, on from the start
        return await press.deny("keys.gone")
    admin = await is_admin(bot, chat, press.user_id)
    async with db.session() as session:
        link_row = await session.get(KeyChat, (key.id, chat.id))
    if (
        link_row is None or link_row.state == "off"
    ):  # never offered here, or unplugged: only its owner offers it
        await press.strip()
        return await press.deny("keys.gone")
    if data.action == "here_on":
        if not admin:
            return await press.deny("keys.admins_only")
        if link_row.state == "on":
            return await press.strip()
        if link_row.paused_by == key.user_id and press.user_id != key.user_id:
            return await press.deny("keys.owner_paused")
        await _enable(
            bot,
            db,
            settings,
            chat,
            key,
            owner,
            press.user_id,
            chat.locale or press.locale,
            card=None if press.ephemeral else press,
        )
        await press.toast("keys.turned_on")
        if press.ephemeral:
            await _redraw_panel(press, db, settings, chat, admin)
        return None
    if not admin and press.user_id != key.user_id:
        return await press.deny("keys.owner_or_admin")
    state = "paused" if data.action == "here_pause" else "off"
    async with db.transaction() as session:
        row = await session.get(KeyChat, (key.id, chat.id), with_for_update=True)
        if row is None or row.state == state:
            return await press.strip()
        was_offered = row.state == "offered"
        row.state, row.off_noticed_at = state, datetime.now(UTC)
        row.paused_by = press.user_id if state == "paused" else None
    public = chat.locale or press.locale
    if was_offered and state == "off":
        return await press.edit(t(public, "keys.declined"))
    if press.ephemeral:  # the /settings keys page: redrawn as it is now
        return await _redraw_panel(press, db, settings, chat, admin)
    key_text = "keys.paused_here" if state == "paused" else "keys.unplugged_here"
    await press.edit(t(public, key_text, who=owner_name(owner, public), brand=brand(key.provider)))


async def _redraw_panel(press: Press, db: Database, settings: Settings, chat: Chat, admin: bool) -> None:
    from derp.settings.panel import render  # the panel renders this page among its own

    async with db.session() as session:
        user = await session.get(User, press.user_id)
        text, keyboard = await render(
            session, press.bot, settings, chat, user, press.locale, "keys", admin=admin, viewer=press.user_id
        )
    await press.edit(text, keyboard)


async def on_press_key(
    press: Press,
    data: KeyAction,
    db: Database,
    settings: Settings,
    funder: Funder,
    management: OpenRouterManagement | None,
) -> None:
    if data.action.startswith("here_"):
        return await _chat_press(press, data, db, settings)
    if press.message.chat.type != "private":
        return await press.deny("keys.gone")
    match data.action:
        case "page":
            return await _show_page(press, db, settings, funder, data.key)
        case "plug":
            await press.toast()
            return await open_plug(press.bot, press.query.from_user, db, settings, None)
        case "use":
            async with db.session() as session:
                key = await _own_key(session, press.user_id, data.key)
            chat_id = chat_from_ref(settings, data.value)
            if key is None or chat_id is None:
                return await press.deny("keys.gone")
            async with db.transaction() as session:
                await session.execute(delete(KeyRequest).where(KeyRequest.user_id == press.user_id))
            return await offer(press, db, settings, key, chat_id)
        case "cap" if data.value == "other":
            async with db.transaction() as session:
                if await _own_key(session, press.user_id, data.key) is None:
                    return await press.deny("keys.gone")
                request = await _request(session, settings, press.user_id)
                chat_id = request.chat_id if request is not None and request.key_id == data.key else None
                await _set_request(session, press.user_id, "cap", chat_id=chat_id, key_id=data.key)
            return await press.edit(t(press.locale, "keys.cap_other", max=settings.keys.max_cap_usd))
        case "cap" if data.value.isdigit():
            await _set_cap(
                press.bot,
                db,
                settings,
                funder,
                Target(press.chat_id),
                press.user_id,
                data.key,
                Decimal(data.value),
                press.locale,
                edit=press,
            )
            return None
        case "cap":
            async with db.session() as session:
                key = await _own_key(session, press.user_id, data.key)
            if key is None:
                return await press.deny("keys.gone")
            return await press.edit(
                t(press.locale, "keys.cap_page", cap=f"{key.cap_usd:g}"),
                _cap_buttons(settings, press.locale, key.id),
            )
        case "models":
            async with db.session() as session:
                key = await _own_key(session, press.user_id, data.key)
                if key is None:
                    return await press.deny("keys.gone")
                text, keyboard = await _model_page(session, settings, funder, key, press.locale)
            return await press.edit(text, keyboard)
        case "model":
            return await _set_model(press, db, settings, funder, management, data.key, data.value)
        case "pause" | "resume":
            async with db.transaction() as session:
                key = await _own_key(session, press.user_id, data.key)
                if key is None:
                    return await press.deny("keys.gone")
                if data.action == "pause":
                    await pause(session, key, "owner", None)
                    key.noticed_at = datetime.now(UTC)  # their own doing: nothing to tell them
                else:
                    await resume(session, key)
            return await _show_page(press, db, settings, funder, data.key)
        case "unplug" if data.value.lstrip("-").isdigit():
            async with db.transaction() as session:
                if await _own_key(session, press.user_id, data.key) is None:
                    return await press.deny("keys.gone")
                row = await session.get(KeyChat, (data.key, int(data.value)), with_for_update=True)
                if row is not None:
                    row.state, row.off_noticed_at = "off", datetime.now(UTC)
            return await _show_page(press, db, settings, funder, data.key)
        case "remove":
            confirm = button(
                t(press.locale, "keys.remove_confirm"),
                KeyAction(action="remove_yes", key=data.key),
                style="danger",
            )
            back = button(t(press.locale, "settings.back"), KeyAction(action="page", key=data.key))
            return await press.edit(t(press.locale, "keys.remove_page"), markup(confirm, back))
        case "remove_yes":
            async with db.session() as session:
                key = await _own_key(session, press.user_id, data.key)
            if key is None or management is None:
                return await press.deny("keys.gone")
            try:
                await take_out(db, management, key)
            except OpenRouterError:
                return await press.deny("keys.remove_failed")
            return await press.edit(t(press.locale, "keys.removed"))
        case _:
            return await press.deny("keys.gone")


async def _show_page(press: Press, db: Database, settings: Settings, funder: Funder, key_id: int) -> None:
    async with db.session() as session:
        keys = await live_keys(session, press.user_id)
        chosen = (
            next((k for k in keys if k.id == key_id), None)
            if key_id
            else (keys[0] if len(keys) == 1 else None)
        )
        if chosen is not None:
            text, keyboard = await key_page(
                session, settings, funder, chosen, press.locale, many=len(keys) > 1
            )
        else:
            text, keyboard = my_keys(keys, press.locale)
    await press.edit(text, keyboard)


def my_keys(keys: list[ContributedKey], locale: str) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = [
        button(
            t(locale, "keys.key_button", brand=brand(k.provider), label=literal(k.label)),
            KeyAction(action="page", key=k.id),
        )
        for k in keys
    ]
    plug = button(t(locale, "settings.keys_plug"), KeyAction(action="plug", key=0))
    return t(locale, "keys.mine" if keys else "keys.mine_none"), markup(*rows, plug)


async def _chat_state(
    session: AsyncSession, key: ContributedKey, link: KeyChat, locale: str, now: datetime
) -> str:
    """A key's state in one chat; on but idle when a key turned on earlier answers there first."""
    first = next(iter(await candidates(session, link.chat_id, now)), None) if link.state == "on" else None
    if first is None or first.id == key.id:
        return t(locale, f"keys.chat_{link.state}")
    if first.user_id == key.user_id:
        return t(locale, "keys.chat_waiting_own", label=literal(first.label))
    return t(locale, "keys.chat_waiting", who=owner_name(await session.get(User, first.user_id), locale))


async def key_page(
    session: AsyncSession,
    settings: Settings,
    funder: Funder,
    key: ContributedKey,
    locale: str,
    *,
    many: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The owner's page for one key: model, cap and this cycle's spend, its state, and its chats."""
    now = datetime.now(UTC)
    priced = await prices(session, settings, key.model)
    if key.dead_at is not None:
        state = t(locale, "keys.state_dead")
    elif key.paused == "owner":
        state = t(locale, "keys.state_paused")
    elif key.paused in ("cap", "exhausted") and key.paused_until and key.paused_until > now:
        state = t(locale, f"keys.state_{key.paused}", date=when(key.paused_until))
    else:
        state = t(locale, "keys.state_on")
    lines = [
        t(
            locale,
            "keys.page",
            brand=brand(key.provider),
            label=literal(key.label),
            model=model_label(settings, key.model),
            price=per_reply(funder, priced) if priced else "?",
            cap=f"{key.cap_usd:g}",
            spent=f"{await spent(session, key, now):.2f}",
            since=when(cycle_start(key.cycle_anchor, now)),
            reset=when(next_cycle(key.cycle_anchor, now)),
            state=state,
        )
    ]
    chats = (
        await session.execute(
            select(KeyChat, Chat)
            .join(Chat, Chat.id == KeyChat.chat_id)
            .where(KeyChat.key_id == key.id, KeyChat.state != "off", Chat.type != "private")
            .order_by(KeyChat.created_at)
        )
    ).all()
    unplug: list[InlineKeyboardButton] = []
    for row, chat in chats:
        title = literal(one_line(chat.title or "?", 40))
        lines.append(
            t(
                locale,
                "keys.chat_line",
                title=title,
                spent=f"{await spent(session, key, now, chat.id):.2f}",
                state=await _chat_state(session, key, row, locale, now),
            )
        )
        unplug.append(
            button(
                t(locale, "keys.unplug_button", title=one_line(chat.title or "?", 24)),
                KeyAction(action="unplug", key=key.id, value=str(chat.id)),
            )
        )
    if not chats:
        lines.append(t(locale, "keys.no_chats"))
    paused = key.paused == "owner"
    keyboard = markup(
        [
            button(t(locale, "keys.model_change"), KeyAction(action="models", key=key.id)),
            button(t(locale, "keys.cap_change"), KeyAction(action="cap", key=key.id)),
        ],
        [
            button(
                t(locale, "keys.resume" if paused else "keys.pause"),
                KeyAction(action="resume" if paused else "pause", key=key.id),
            ),
            button(t(locale, "keys.remove"), KeyAction(action="remove", key=key.id), style="danger"),
        ],
        *unplug,
        button(t(locale, "keys.all_keys"), KeyAction(action="page", key=0)) if many else None,
    )
    return "\n".join(lines), keyboard


# The /settings keys page of a group (rendered inside the panel).


async def chat_keys(session: AsyncSession, chat_id: int) -> list[tuple[KeyChat, Plugged]]:
    """Keys plugged into a chat (not unplugged), in serving order."""
    rows = (
        await session.execute(
            select(KeyChat, ContributedKey, User)
            .join(ContributedKey, ContributedKey.id == KeyChat.key_id)
            .outerjoin(User, User.id == ContributedKey.user_id)
            .where(KeyChat.chat_id == chat_id, KeyChat.state != "off", ContributedKey.removed_at.is_(None))
            .order_by(KeyChat.enabled_at.nulls_last(), KeyChat.created_at)
        )
    ).all()
    return [(row, Plugged(key, owner)) for row, key, owner in rows]


def chat_keys_line(settings: Settings, keys: list[tuple[KeyChat, Plugged]], locale: str) -> str:
    on = [p for row, p in keys if row.state == "on"]
    if not on:
        return t(locale, "settings.keys_off")
    named = ", ".join(
        t(
            locale,
            "settings.key_name",
            who=owner_name(p.owner, locale),
            brand=brand(p.key.provider),
            model=model_label(settings, p.key.model),
        )
        for p in on
    )
    return t(locale, "settings.keys_on", keys=named)


def chat_keys_page(
    settings: Settings, keys: list[tuple[KeyChat, Plugged]], viewer: int | None, admin: bool, locale: str
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    lines = [t(locale, "settings.keys_page")]
    rows: list[list[InlineKeyboardButton]] = []
    for row, plugged in keys:
        key = plugged.key
        lines.append(
            t(
                locale,
                "settings.key_line",
                who=owner_name(plugged.owner, locale),
                brand=brand(key.provider),
                model=model_label(settings, key.model),
                state=t(locale, f"keys.chat_{row.state}"),
            )
        )
        buttons: list[InlineKeyboardButton] = []
        if admin and row.state != "on":
            buttons.append(button(t(locale, "keys.turn_on"), KeyAction(action="here_on", key=key.id)))
        if admin and row.state == "on":
            buttons.append(button(t(locale, "keys.pause_button"), KeyAction(action="here_pause", key=key.id)))
        if admin or viewer == key.user_id:
            buttons.append(button(t(locale, "keys.remove_button"), KeyAction(action="here_off", key=key.id)))
        if buttons:
            rows.append(buttons)
    if not keys:
        lines.append(t(locale, "settings.keys_none"))
    return "\n".join(lines), rows


def build_router() -> Router:
    router = Router(name="keys")
    router.message.register(on_pasted, PastedKey())
    router.edited_message.register(on_pasted, PastedKey())  # a key edited in goes the same way
    router.message.register(on_cap_amount, F.chat.type == "private", CapAmount())
    on_press(router, KeyAction)(on_press_key)
    return router
