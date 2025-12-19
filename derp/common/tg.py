from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Chat,
    Message,
    PhotoSize,
    TelegramObject,
    Update,
    User,
)

from .sanitize import sanitize_for_telegram
from .utils import one_liner


def user_info(user: User, sender_chat: Chat | None = None) -> str:
    if sender_chat:
        return chat_info(sender_chat)

    last_name = " " + user.last_name if user.last_name else ""
    username = ", @" + user.username if user.username else ""
    language_code = ", " + user.language_code if user.language_code else ""
    return f"{user.first_name}{last_name} ({user.id}{username}{language_code})"


def chat_info(chat: Chat) -> str:
    if chat.type == "private":
        return "private"

    username = ", @" + chat.username if chat.username else ""
    return f"{chat.type} | {chat.title} ({chat.id}{username})"


def message_info(message: Message) -> str:
    prefix = f"{message.message_id} | "
    if message.text:
        return prefix + one_liner(message.text, cut_len=50)
    return prefix + f"type: {message.content_type}"


def decompose_update(
    update: Update,
) -> tuple[TelegramObject, User | None, Chat | None, Chat | None, str]:
    user, sender_chat, chat = None, None, None

    if f := update.message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_message:
        user = f.from_user
        sender_chat = f.sender_chat
        chat = f.chat
        info = message_info(f) + " [edited]"
    elif f := update.channel_post:
        chat = f.chat
        info = message_info(f)
    elif f := update.edited_channel_post:
        chat = f.chat
        info = message_info(f) + " [edited]"
    elif f := update.inline_query:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.chosen_inline_result:
        user = f.from_user
        info = one_liner(f.query, cut_len=50)
    elif f := update.callback_query:
        if f.message:
            chat = f.message.chat
        user = f.from_user
        info = f.data
    elif f := update.shipping_query:
        user = f.from_user
        info = f.model_dump_json(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )
    elif f := update.pre_checkout_query:
        user = f.from_user
        info = f.model_dump_json(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )
    elif f := update.poll:
        info = (
            f"{one_liner(f.question, cut_len=50)} ({f.id}),"
            f" {[o.text for o in f.options]}, {f.total_voter_count} voter(s)"
        )
    elif f := update.poll_answer:
        user = f.user
        info = f"{f.option_ids} ({f.poll_id})"
    elif f := (update.chat_member or update.my_chat_member):
        user = f.from_user
        chat = f.chat
        info = (
            f"{user_info(f.new_chat_member.user)}: {f.old_chat_member.status} ->"
            f" {f.new_chat_member.status}"
        )
    else:
        f = update
        info = update.model_dump_json(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )

    return f, user, sender_chat, chat, info


async def profile_photo(u: User) -> PhotoSize | None:
    photos = await u.get_profile_photos(limit=1)
    if photos.total_count < 1:
        return None
    return photos.photos[0][-1]


async def create_sensitive_url_from_file_id(bot: Bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    return bot.session.api.file_url(bot.token, file.file_path)


def extract_attachment_info(
    message: Message,
) -> tuple[str | None, str | None, str | None]:
    attachment_type = None
    attachment_file_id = None
    attachment_filename = None

    if a := message.photo:
        attachment_type = "photo"
        attachment_file_id = a[-1].file_id
    elif a := message.audio:
        attachment_type = "audio"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.voice:
        attachment_type = "voice"
        attachment_file_id = a.file_id
    elif a := message.sticker:
        attachment_type = "sticker"
        attachment_file_id = a.file_id
    elif a := message.video:
        attachment_type = "video"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.video_note:
        attachment_type = "video_note"
        attachment_file_id = a.file_id
    elif a := message.animation:
        attachment_type = "animation"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name
    elif a := message.document:
        attachment_type = "document"
        attachment_file_id = a.file_id
        attachment_filename = a.file_name

    return attachment_type, attachment_file_id, attachment_filename


async def extract_attachment_info_with_url(
    message: Message,
) -> tuple[str | None, str | None, str | None, str | None]:
    attachment_type, attachment_file_id, attachment_filename = extract_attachment_info(
        message
    )
    attachment_url = None

    if attachment_file_id:
        attachment_url = await create_sensitive_url_from_file_id(
            message.bot, attachment_file_id
        )

    return attachment_type, attachment_file_id, attachment_filename, attachment_url


def extract_attachment_file_id(message: Message) -> str | None:
    _, attachment_file_id, _ = extract_attachment_info(message)

    return attachment_file_id


async def reply_with_attachment(
    message: Message,
    text: str,
    attachment_type: str,
    attachment_file_id: str,
    attachment_url_fallback: str | None = None,
):
    """Reply to a message with an attachment and caption.

    Sanitizes the caption text before sending.
    """
    prepared_caption = sanitize_for_telegram(text) if text else None

    async def send(method):
        try:
            return await method(
                attachment_file_id,
                caption=prepared_caption,
                parse_mode="HTML" if prepared_caption else None,
            )
        except TelegramBadRequest:
            if attachment_url_fallback:
                return await method(
                    attachment_url_fallback,
                    caption=prepared_caption,
                    parse_mode="HTML" if prepared_caption else None,
                )
            raise

    match attachment_type:
        case "photo":
            return await send(message.reply_photo)
        case "audio":
            return await send(message.reply_audio)
        case "voice":
            return await send(message.reply_voice)
        case "sticker":
            return await send(message.reply_sticker)
        case "video":
            return await send(message.reply_video)
        case "video_note":
            return await send(message.reply_video_note)
        case "animation":
            return await send(message.reply_animation)
        case "document":
            return await send(message.reply_document)

    return await message.reply(text, disable_web_page_preview=False)
