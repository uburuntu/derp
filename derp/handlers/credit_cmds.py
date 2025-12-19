"""Credit management commands.

Provides commands for users to:
- /credits - Check their credit balance
- /buy - Purchase credits with Telegram Stars
"""

from __future__ import annotations

import logfire
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.db import get_db_manager
from derp.db.credits import get_balances
from derp.models import Chat as ChatModel
from derp.models import User as UserModel

router = Router(name="credits")


@router.message(Command("credits", "balance", "bal"))
async def show_credits(
    message: Message,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    """Show the user's credit balance."""
    if not user:
        return await message.reply(_("😅 Could not find your user info."))

    db = get_db_manager()
    async with db.read_session() as session:
        if chat_settings:
            chat_credits, user_credits = await get_balances(
                session, user.telegram_id, chat_settings.telegram_id
            )
        else:
            chat_credits = 0
            user_credits = 0

    logfire.info(
        "credits_checked",
        user_id=user.telegram_id,
        chat_id=chat_settings and chat_settings.telegram_id,
        user_credits=user_credits,
        chat_credits=chat_credits,
    )

    # Build response message
    parts = [_("💰 **Your Credits**\n")]

    if chat_settings and chat_settings.type != "private":
        parts.append(
            _("🏠 Chat pool: **{credits}** credits").format(credits=chat_credits)
        )
        parts.append(
            _("👤 Personal: **{credits}** credits\n").format(credits=user_credits)
        )
        if chat_credits > 0:
            parts.append(_("✅ Chat credits will be used first."))
        elif user_credits > 0:
            parts.append(_("✅ Your personal credits will be used."))
        else:
            parts.append(_("💡 No credits! Use /buy to get some."))
    else:
        parts.append(
            _("👤 Balance: **{credits}** credits\n").format(credits=user_credits)
        )
        if user_credits > 0:
            parts.append(_("✅ You have credits for premium features!"))
        else:
            parts.append(_("💡 No credits! Use /buy to get some."))

    return await message.reply("\n".join(parts), parse_mode="Markdown")


@router.message(Command("buy", "purchase", "shop"))
async def show_buy_options(
    message: Message,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    """Show credit purchase options with inline payment buttons.

    Displays available credit packs and inline buttons to purchase.
    Users can buy credits for themselves or for the chat pool.
    """
    if not user:
        return await message.reply(_("😅 Could not find your user info."))

    # Import here to avoid circular imports
    from derp.handlers.payments import CREDIT_PACKS, build_buy_keyboard

    logfire.info(
        "buy_menu_shown",
        user_id=user.telegram_id,
        chat_id=chat_settings and chat_settings.telegram_id,
    )

    # Build message with pack info
    parts = [
        _("🛒 **Credit Packs**\n"),
        _("Buy credits with Telegram Stars ⭐\n"),
    ]

    for pack in CREDIT_PACKS.values():
        if pack.bonus_pct > 0:
            parts.append(
                _(
                    "• **{name}**: {stars} ⭐ → {credits} credits (+{bonus}% bonus)"
                ).format(
                    name=pack.name,
                    stars=pack.stars,
                    credits=pack.credits,
                    bonus=pack.bonus_pct,
                )
            )
        else:
            parts.append(
                _("• **{name}**: {stars} ⭐ → {credits} credits").format(
                    name=pack.name, stars=pack.stars, credits=pack.credits
                )
            )

    parts.extend(
        [
            "",
            _("**What can you do with credits?**"),
            _("• 1 credit = 1 AI message (better quality model)"),
            _("• 5 credits = 1 image generation"),
            _("• 10 credits = 1 deep thinking (/think)"),
            "",
            _("👇 **Tap a button to buy:**"),
        ]
    )

    # Build keyboard - personal credits by default
    # In group chats, also offer chat credits option
    keyboard = build_buy_keyboard(chat_id=None)  # Personal credits

    return await message.reply(
        "\n".join(parts),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@router.message(Command("buy_chat", "buychat"))
async def show_buy_chat_options(
    message: Message,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    """Buy credits for the chat pool (group chats only).

    Chat credits are shared among all members and used first.
    """
    if not user:
        return await message.reply(_("😅 Could not find your user info."))

    if not chat_settings or chat_settings.type == "private":
        return await message.reply(
            _(
                "💡 This command is for group chats only.\nUse /buy for personal credits."
            )
        )

    from derp.handlers.payments import build_buy_keyboard

    logfire.info(
        "buy_chat_menu_shown",
        user_id=user.telegram_id,
        chat_id=chat_settings.telegram_id,
    )

    parts = [
        _("🏠 **Buy Chat Credits**\n"),
        _("Credits for this chat's shared pool.\n"),
        _("Everyone in the chat can use them!\n"),
        "",
        _("👇 **Tap a button to buy:**"),
    ]

    keyboard = build_buy_keyboard(chat_id=chat_settings.telegram_id)

    return await message.reply(
        "\n".join(parts),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@router.message(Command("think"))
async def handle_think(
    message: Message,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    """Handle /think command for deep reasoning using Gemini 3 Pro.

    Uses the PREMIUM model tier (gemini-3-pro-preview) for complex
    reasoning tasks with extended thinking capabilities.

    Reference: https://ai.google.dev/gemini-api/docs/models#gemini-3
    """

    from derp.credits import CreditService
    from derp.llm import AgentDeps, create_chat_agent
    from derp.llm.providers import ModelTier

    prompt = message.text
    if prompt:
        prompt = prompt.removeprefix("/think").strip()

    if not prompt:
        return await message.reply(
            _(
                "🧠 **Deep Thinking Mode**\n\n"
                "Use Gemini 3 Pro for complex math, logic puzzles, "
                "or problems that need careful analysis.\n\n"
                "Usage: /think <your problem or question>"
            ),
            parse_mode="Markdown",
        )

    # Check credits
    if not user or not chat_settings:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    db = get_db_manager()
    async with db.session() as session:
        service = CreditService(session)
        result = await service.check_tool_access(
            user_id=user.id,
            chat_id=chat_settings.id,
            user_telegram_id=user.telegram_id,
            chat_telegram_id=chat_settings.telegram_id,
            tool_name="think_deep",
        )

        if not result.allowed:
            return await message.reply(
                _(
                    "🧠 Deep thinking requires credits.\n\n"
                    "✨ {reason}\n\n"
                    "💡 Use /buy to get credits!"
                ).format(reason=result.reject_reason),
                parse_mode="Markdown",
            )

        # Send typing indicator while thinking
        await message.bot.send_chat_action(message.chat.id, "typing")

        logfire.info(
            "think_command_started",
            user_id=user.telegram_id,
            prompt_length=len(prompt),
        )

        try:
            # Create PREMIUM agent (Gemini 3 Pro)
            agent = create_chat_agent(ModelTier.PREMIUM)

            # Build deps for the agent
            deps = AgentDeps(
                message=message,
                db=db,
                bot=message.bot,
                chat=chat_settings,
                user=user,
                tier=ModelTier.PREMIUM,
            )

            # Run with thinking-optimized prompt
            thinking_prompt = (
                "You are in deep thinking mode. Take your time to carefully analyze "
                "the problem step by step. Show your reasoning process clearly.\n\n"
                f"**Problem:**\n{prompt}"
            )

            agent_result = await agent.run(thinking_prompt, deps=deps)

            # Deduct credits after successful generation
            idempotency_key = f"think:{chat_settings.telegram_id}:{message.message_id}"
            await service.deduct(
                result,
                user_id=user.id,
                chat_id=chat_settings.id,
                idempotency_key=idempotency_key,
            )

            logfire.info(
                "think_command_completed",
                user_id=user.telegram_id,
                response_length=len(agent_result.output),
            )

            # Send response with markdown, fallback to plain text
            try:
                return await message.reply(
                    f"🧠 **Deep Thinking Result:**\n\n{agent_result.output}",
                    parse_mode="Markdown",
                )
            except Exception:
                return await message.reply(
                    f"🧠 Deep Thinking Result:\n\n{agent_result.output}",
                    parse_mode=None,
                )

        except Exception:
            logfire.exception("think_command_failed", user_id=user.telegram_id)
            return await message.reply(
                _("😅 Something went wrong during deep thinking. Please try again.")
            )
