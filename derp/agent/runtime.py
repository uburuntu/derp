"""One invocation, end to end: fund → window → model run → deliver → record.

A run is recorded before the model is called and closed exactly once, with
whatever provider responses happened (so failed runs are still ledgered).
Hard caps bound every run: tool calls, output tokens per request and wall
time. Delivery goes through the Telegram sender; Derp's delivered messages are
stored as history only after Telegram accepted them.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message as TgMessage
from pydantic_ai import Agent, CancellationToken, RunCancelled, RunContext, capture_run_messages
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    InstructionPart,
    ModelMessage,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    repair_messages,
)
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.context import Window, build_window
from derp.agent.deps import Funding, RunDeps
from derp.agent.media import as_content, inspect, note_images, referenced_media
from derp.agent.persona import PERSONA, situation
from derp.agent.progress import DraftProgress, Progress, ReactionProgress, stage_for_tool
from derp.agent.runs import finish_run, record_output, start_run
from derp.agent.tools import FAMILIES
from derp.agent.toolsets import toolsets_for
from derp.config import Settings
from derp.db.engine import Database
from derp.history.capture import record_outgoing
from derp.history.models import Chat, ForumTopic, Message
from derp.history.render import chat_time, estimate_tokens, load_views, render_line
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.ledger import cancelled_cost, estimate_cost
from derp.inference.models import ModelPin
from derp.inference.openrouter import chat_settings
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole, RolePin
from derp.locales import resolve, t
from derp.ops.observability import record_run_failure
from derp.telegram.media import MediaGateway
from derp.telegram.sender import Target, send_markdown

log = logging.getLogger("derp.agent")

ModelFactory = Callable[[RolePin], Model]
Funder = Callable[[TgMessage, Chat], Awaitable[Funding]]


async def sponsored_funder(_message: TgMessage, _chat: Chat) -> Funding:
    """Placeholder until wallets exist: every run is creator-sponsored on the cheap chat model."""
    return Funding.SPONSORED


@dataclass(frozen=True, slots=True)
class Outcome:
    run_id: int
    status: str
    delivered: tuple[int, ...]


class Runtime:
    def __init__(
        self,
        *,
        db: Database,
        bot: Bot,
        settings: Settings,
        pins: PinStore,
        model_factory: ModelFactory,
        gateway: MediaGateway,
        api: OpenRouterAPI,
        funder: Funder = sponsored_funder,
    ) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self.pins = pins
        self.model_factory = model_factory
        self.funder = funder
        self.gateway = gateway
        self.api = api
        self.active: dict[tuple[int, int], CancellationToken] = {}  # (chat, draft) → the run's stop switch
        self.agent = Agent[RunDeps, str](output_type=str, deps_type=RunDeps, instructions=PERSONA)

    def window_budget(self, funding: Funding) -> int:
        windows = self.settings.context_windows
        return {Funding.PAID: windows.paid, Funding.SPONSORED: windows.sponsored, Funding.FREE: windows.free}[
            funding
        ]

    async def answer(self, message: TgMessage, chat: Chat, *, trigger: str = "message") -> Outcome:
        asker = message.from_user
        if asker is None:
            raise ValueError("an invocation needs a sender")
        locale = resolve(asker.language_code)
        tz = chat_time(chat)
        target = Target.reply_to_message(message)
        funding = await self.funder(message, chat)
        pin = await self.pins.get(funding.role)
        async with self.db.transaction() as session:
            run = await start_run(
                session,
                chat_id=chat.id,
                user_id=asker.id,
                trigger=trigger,
                role=funding.role.value,
                funding=funding.value,
                thread_id=target.thread_id,
                trigger_message_id=message.message_id or None,
            )
            window = await build_window(
                session,
                chat_id=chat.id,
                thread_id=target.thread_id,
                before_message_id=message.message_id or 2**62,
                budget=self.window_budget(funding),
                tz=tz,
            )
            prompt = await current_prompt(session, message, window, tz)
            reply_to = message.reply_to_message.message_id if message.reply_to_message else None
            images = await referenced_media(session, chat.id, message.message_id, reply_to)
            topic = await session.get(ForumTopic, (chat.id, target.thread_id)) if target.thread_id else None
        inspected = await inspect(self.gateway, images)
        model = self.model_factory(pin)
        deps = RunDeps(
            db=self.db,
            bot=self.bot,
            settings=self.settings,
            chat=chat,
            user_id=asker.id,
            asker=asker.full_name,
            locale=locale,
            thread_id=target.thread_id,
            trigger_message_id=message.message_id,
            run_id=run.id,
            funding=funding,
            target=target,
            tz=tz,
            gateway=self.gateway,
            pin=pin,
            model=model,
        )
        brief = situation(
            chat=chat,
            asker=asker.full_name,
            now=datetime.now(UTC),
            tz=tz,
            topic=topic.name if topic else None,
        )
        caps = self.settings.run_caps
        progress: Progress = (
            DraftProgress(self.bot, target) if chat.type == "private" else ReactionProgress(self.bot, target)
        )
        stream = _Stream(progress)
        token = CancellationToken()
        output = ""
        error: BaseException | None = None
        stopped = False
        # Pydantic AI normalizes history (merging adjacent turns) before the run; pre-normalizing here
        # keeps the captured list aligned so the run's own messages slice out exactly.
        history = repair_messages(window.messages)
        new_messages: list[ModelMessage] = []
        await progress.start()
        if progress.draft_id is not None:
            self.active[(chat.id, progress.draft_id)] = token
        try:
            with capture_run_messages() as captured:
                try:
                    async with asyncio.timeout(caps.timeout_seconds):
                        result = await self.agent.run(
                            [prompt, *as_content(inspected)] if pin.vision and inspected else prompt,
                            message_history=history,
                            deps=deps,
                            model=model,
                            instructions=InstructionPart(brief, dynamic=True),
                            model_settings=chat_settings(pin, max_tokens=caps.output_tokens),
                            usage_limits=UsageLimits(
                                request_limit=caps.tool_calls + 2, tool_calls_limit=caps.tool_calls
                            ),
                            toolsets=toolsets_for(deps, FAMILIES),
                            event_stream_handler=stream.handle,
                            cancellation_token=token,
                        )
                    output = result.output
                    new_messages = result.new_messages()
                except RunCancelled as cancelled:  # the user pressed stop: keep what was written so far
                    stopped = True
                    new_messages = list(cancelled.new_messages())
                    output = stream.partial
                except Exception as exc:  # noqa: BLE001 - any failure closes the run and gets an apology
                    error = exc
                    cause = (
                        exc.__cause__ if isinstance(exc, TimeoutError) else None
                    )  # timeout wraps the cancel
                    interrupted = RunCancelled.from_cancellation(cause) if cause is not None else None
                    new_messages = (
                        list(interrupted.new_messages()) if interrupted else list(captured[len(history) :])
                    )
        finally:
            if progress.draft_id is not None:
                self.active.pop((chat.id, progress.draft_id), None)
            await progress.close()
        await price_interrupted(self.api, self.db, new_messages, funding.role, window.tokens)
        if error is None and (output.strip() or not (deps.delivered or stopped)):
            try:
                sent = await send_markdown(self.bot, target, output, trusted=False)
            except TelegramAPIError as exc:
                error = exc
            else:
                deps.delivered.extend(m.message_id for m in sent)
                async with self.db.transaction() as session:
                    await record_outgoing(session, sent)
        status = "failed" if error is not None else "cancelled" if stopped else "completed"
        async with self.db.transaction() as session:
            stored = await session.merge(run)
            for message_id in deps.delivered:
                record_output(session, stored, message_id, "message")
            finish_run(
                session,
                stored,
                new_messages,
                status=status,
                context_tokens=window.tokens,
                error_type=type(error).__name__ if error else None,
            )
        await note_images(
            self.db,
            model,
            pin,
            inspected,
            run_id=run.id,
            funding=funding.value,
            chat_id=chat.id,
            user_id=asker.id,
        )
        if error is not None:
            record_run_failure(run.id, error, prompt)
            if not isinstance(error, TelegramAPIError) and not deps.delivered:
                await self._apologize(target, locale, error)
        return Outcome(run.id, status, tuple(deps.delivered))

    def stop(self, chat_id: int, draft_id: int) -> bool:
        """The user pressed stop on a draft; cancel the run that owns it."""
        token = self.active.get((chat_id, draft_id))
        if token is None:
            return False
        token.cancel()
        return True

    async def _apologize(self, target: Target, locale: str, error: BaseException) -> None:
        key = "agent.timeout" if isinstance(error, TimeoutError) else "agent.failed"
        try:
            await send_markdown(self.bot, target, t(locale, key))
        except TelegramAPIError:
            log.info("apology not delivered")


class _Stream:
    """Feeds streamed events to the progress indicator and remembers the latest answer text."""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.partial = ""

    async def handle(self, _ctx: RunContext[RunDeps], events: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in events:
            if isinstance(event, PartStartEvent):
                if event.index == 0:  # a new model response: its text replaces the previous one's
                    self.partial = ""
                if isinstance(event.part, TextPart):
                    self.partial += event.part.content
                    await self.progress.text(self.partial)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                self.partial += event.delta.content_delta
                await self.progress.text(self.partial)
            elif isinstance(event, FunctionToolCallEvent):
                await self.progress.stage(stage_for_tool(event.part.tool_name))


async def price_interrupted(
    api: OpenRouterAPI, db: Database, messages: list[ModelMessage], role: ModelRole, context_tokens: int
) -> None:
    """Responses cut off mid-stream never got a cost; ask OpenRouter, else estimate from the pin's prices."""
    cut = [
        m
        for m in messages
        if isinstance(m, ModelResponse)
        and m.state == "interrupted"
        and not (m.provider_details or {}).get("cost")
    ]
    if not cut:
        return
    async with db.session() as session:
        prices = await session.get(ModelPin, role.value)
    for response in cut:
        cost: Decimal | None = None
        if response.provider_response_id:
            with contextlib.suppress(OpenRouterError):
                cost = await cancelled_cost(api, response.provider_response_id)
        if cost is None and prices is not None and prices.price_prompt is not None:
            written = sum(estimate_tokens(p.content) for p in response.parts if isinstance(p, TextPart))
            cost = estimate_cost(
                response.usage.input_tokens or context_tokens,
                response.usage.output_tokens or written,
                float(prices.price_prompt),
                float(prices.price_completion or 0),
            )
        if cost is not None:
            response.provider_details = {
                **(response.provider_details or {}),
                "cost": float(cost),
                "estimated": True,
            }


async def current_prompt(session: AsyncSession, message: TgMessage, window: Window, tz: tzinfo) -> str:
    """The current message, plus the message it replies to when that isn't already in the window."""
    row = await session.get(Message, (message.chat.id, message.message_id)) if message.message_id else None
    asker = message.from_user.full_name if message.from_user else "someone"
    if row is not None:
        line = render_line((await load_views(session, [row]))[0], tz)
    else:
        line = f"{asker}: {message.text or message.caption or ''}"
    parts: list[str] = []
    reply = message.reply_to_message
    if reply is not None and (
        window.oldest_message_id is None or reply.message_id < window.oldest_message_id
    ):
        replied = await session.get(Message, (message.chat.id, reply.message_id))
        if replied is not None and replied.deleted_at is None:
            parts.append("Replying to: " + render_line((await load_views(session, [replied]))[0], tz))
        elif reply.text or reply.caption:
            author = reply.from_user.full_name if reply.from_user else "someone"
            parts.append(f"Replying to {author}: {reply.text or reply.caption}")
    parts.append(f"<current_message>\n{line}\n</current_message>")
    return "\n".join(parts)
