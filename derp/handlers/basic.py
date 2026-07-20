from aiogram import Router, html
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.config import settings

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = _(
        "👋 Hello, {name}!\n\n"
        "I'm Derp, your AI-powered Telegram assistant powered by Google's Gemini.\n\n"
        "I can help you with:\n"
        "• 🤖 AI conversations and questions\n"
        "• 🎨 Image generation and editing (premium)\n"
        "• 📝 Code generation and analysis\n"
        "• 🔍 Web search and research\n"
        "• 💭 Chat memory management\n\n"
        "Type /help to see all available commands, or just start chatting with me!"
    ).format(name=html.quote(message.from_user.full_name))
    return await message.reply(welcome_text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = _(
        "I'm a friendly AI-powered Telegram bot\n\n"
        "Available commands:\n"
        "• /derp - Trigger AI response\n"
        "• /donate [stars] - Support the bot with Telegram Stars (default 20)\n"
        "• /credits - Check your credit balance\n"
        "• /imagine <prompt> - Generate images with AI (premium)\n"
        "• /edit <prompt> - Edit images with AI (premium)\n"
        "• /video [fast|standard] <prompt> - Generate a video with Veo (premium)\n"
        "• /tts <text> - Generate a voice message (premium)\n"
        "• /settings - Show current chat settings\n"
        "• /set_memory <text> - Set LLM memory for this chat\n"
        "• /clear_memory - Clear LLM memory for this chat\n"
        "• /help - Show this help message\n\n"
        "You can also:\n"
        "• Mention me (@DerpRobot) in group chats\n"
        "• Reply to my messages\n"
        "• Use me in private chats\n"
        "• Use inline mode: @DerpRobot <query>"
    )

    # Add debug commands for admins
    if message.from_user and message.from_user.id in settings.admin_ids:
        debug_text = _(
            "\n\n🛠 Debug (admin only):\n"
            "• /debug_credits <n> [chat] - Add credits\n"
            "• /debug_reset [chat] - Reset to 0\n"
            "• /debug_status - Full diagnostic\n"
            "• /debug_refund <id> - Test refund\n"
            "• /debug_tools - List tool pricing\n"
            "• /debug_help - All debug commands"
        )
        help_text += debug_text

    return await message.reply(html.quote(help_text))
