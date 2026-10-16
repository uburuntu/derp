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
import uuid
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message as TgMessage
from pydantic_ai import Agent, CancellationToken, RunCancelled, RunContext, capture_run_messages
from pydantic_ai.exceptions import ModelHTTPError
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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.context import Window, build_window
from derp.agent.deps import RunDeps
from derp.agent.lanes import Lanes
from derp.agent.media import as_content, inspect, note_images, referenced_media
from derp.agent.models import AgentRun
from derp.agent.persona import PERSONA, situation
from derp.agent.progress import DraftProgress, Progress, ReactionProgress, stage_for_tool
from derp.agent.runs import finish_run, record_output, start_run, totals
from derp.agent.tools import FAMILIES
from derp.agent.tools.web import WEB_CAPABILITIES
from derp.agent.toolsets import toolsets_for
from derp.config import Settings
from derp.db.engine import Database
from derp.history.capture import record_outgoing
from derp.history.digests import latest_before
from derp.history.models import Chat, ForumTopic, Message, User
from derp.history.render import chat_time, estimate_tokens, load_views, neutralize, one_line, render_line
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.ledger import cancelled_cost, estimate_cost
from derp.inference.models import ModelPin
from derp.inference.openrouter import chat_settings
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole, RolePin
from derp.locales import resolve, t
from derp.memory.notes import load_memory, notes_block
from derp.money.free import consented, offer
from derp.money.funding import Funder, Funding, Funds
from derp.money.wallets import release
from derp.ops.observability import record_run_failure
from derp.ops.owner import notify_owners
from derp.telegram.media import MediaGateway
from derp.telegram.sender import PartialDeliveryError, Target, send_markdown

log = logging.getLogger("derp.agent")

ModelFactory = Callable[[RolePin], Model]
SILENT = {"", ".", "…"}  # final answers that mean "nothing more to say"
EARLIER_DAYS = 3  # the latest digest before the context window joins the situation if this recent
ALBUM_SETTLE = 1.0  # seconds: album siblings arrive as separate updates; let them be stored first


@dataclass(frozen=True, slots=True)
class Outcome:
    run_id: int | None  # None: nobody could pay, no run was started
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
    ) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self.pins = pins
        self.model_factory = model_factory
        self.funder = Funder(db, settings, notify=lambda text: notify_owners(bot, settings, text))
        self.gateway = gateway
        self.api = api
        self.lanes = Lanes(settings.run_concurrency)
        self.active: dict[tuple[int, int], CancellationToken] = {}  # (chat, draft) → the run's stop switch
        self.in_flight: set[str] = set()  # operation ids of holds a live run still owns
        self.agent = Agent[RunDeps, str](
            output_type=str, deps_type=RunDeps, instructions=PERSONA, capabilities=WEB_CAPABILITIES
        )

    async def answer(self, message: TgMessage, chat: Chat, *, trigger: str = "message") -> Outcome | None:
        """Answer an invocation, queued per chat topic. None when merged into a newer trigger or a duplicate."""
        if message.from_user is None:
            raise ValueError("an invocation needs a sender")
        key = (chat.id, Target.reply_to_message(message).thread_id)
        async with self.lanes.turn(key, message.from_user.id, message.message_id) as go:
            if not go or (message.message_id and await self._answered(chat.id, message.message_id)):
                return None
            return await self._answer(message, chat, trigger=trigger)

    async def _answered(self, chat_id: int, message_id: int) -> bool:
        """A redelivered update (e.g. after a restart) must not be answered twice."""
        async with self.db.session() as session:
            found = await session.scalar(
                select(AgentRun.id).where(
                    AgentRun.chat_id == chat_id, AgentRun.trigger_message_id == message_id
                )
            )
        return found is not None

    async def _answer(self, message: TgMessage, chat: Chat, *, trigger: str) -> Outcome:
        asker = message.from_user
        if asker is None:
            raise ValueError("an invocation needs a sender")
        locale = resolve(asker.language_code)
        if message.media_group_id:
            await asyncio.sleep(ALBUM_SETTLE)
        tz = chat_time(chat)
        target = Target.reply_to_message(message)
        operation_id = (
            f"turn:{chat.id}:{message.message_id}" if message.message_id else f"turn:{uuid.uuid4().hex}"
        )
        private = chat.type == "private"
        async with self.db.session() as session:
            free_allowed = consented(await session.get(User, asker.id)) if private else chat.free_models
        funds = await self.funder.reserve(
            chat_id=chat.id,
            private=private,
            user_id=asker.id,
            operation_id=operation_id,
            free_allowed=free_allowed,
        )
        if funds is None:  # free models not allowed yet: offer them (consent / admin opt-in)
            text, markup = offer(locale, group=not private)
            with contextlib.suppress(TelegramAPIError):
                await send_markdown(self.bot, target, text, reply_markup=markup)
            return Outcome(None, "unfunded", ())
        funding = funds.funding
        run_id: int | None = None
        self.in_flight.add(funds.operation_id)
        try:
            pin = await self.pins.get(funding.role)
            digest_role = ModelRole.CHAT_FREE if funding is Funding.FREE else ModelRole.CHAT_SPONSORED
            digest_pin = await self.pins.get(digest_role)
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
                    wallet_id=funds.wallet_id,
                    reservation_id=funds.reservation_id,
                )
                run_id = run.id
                window = await build_window(
                    session,
                    chat_id=chat.id,
                    thread_id=target.thread_id,
                    before_message_id=message.message_id or 2**62,
                    budget=self.funder.window(funding),
                    tz=tz,
                )
                prompt = await current_prompt(session, message, window, tz)
                reply_to = message.reply_to_message.message_id if message.reply_to_message else None
                images = await referenced_media(session, chat.id, message.message_id, reply_to)
                topic = (
                    await session.get(ForumTopic, (chat.id, target.thread_id)) if target.thread_id else None
                )
                memory = await load_memory(session, chat.id, asker.id)
                earlier = await latest_before(
                    session,
                    chat.id,
                    target.thread_id or 0,
                    window.oldest_message_id or message.message_id or 2**62,
                )
                if earlier is not None and earlier.day < datetime.now(tz).date() - timedelta(
                    days=EARLIER_DAYS
                ):
                    earlier = None
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
                funds=funds,
                target=target,
                tz=tz,
                gateway=self.gateway,
                pin=pin,
                model=model,
                digest_pin=digest_pin,
                digest_model=model if digest_pin == pin else self.model_factory(digest_pin),
                digest_role=digest_role.value,
            )
            brief = situation(
                chat=chat,
                asker=asker.full_name,
                now=datetime.now(UTC),
                tz=tz,
                topic=topic.name if topic else None,
                rules=chat.rules,
                notes=notes_block(memory, asker.full_name),
                earlier=earlier,
                blind=bool(inspected) and not pin.vision,
            )
            caps = self.settings.run_caps
            progress: Progress = (
                DraftProgress(self.bot, target)
                if chat.type == "private"
                else ReactionProgress(self.bot, target)
            )
            token = CancellationToken()
            output = ""
            error: BaseException | None = None
            stopped = False
            # Pydantic AI normalizes history (merging adjacent turns) before the run; pre-normalizing here
            # keeps the captured list aligned so the run's own messages slice out exactly.
            history = repair_messages(window.messages)
            new_messages: list[ModelMessage] = []
            stream = _Stream(progress, token, self.settings, funds, start=len(history))
            await progress.start()
            # Kept locally: the draft id is cleared if drafts fail mid-run.
            draft_key = (chat.id, progress.draft_id) if progress.draft_id is not None else None
            if draft_key is not None:
                self.active[draft_key] = token
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
                            list(interrupted.new_messages())
                            if interrupted
                            else list(captured[len(history) :])
                        )
            finally:
                if draft_key is not None:
                    self.active.pop(draft_key, None)
                if target.reply_to in deps.reacted:
                    progress.keep_reaction()  # Derp's own reaction replaced the status one; leave it
                await progress.close()
            await price_interrupted(self.api, self.db, new_messages, funding.role, window.tokens)
            silent = output.strip() in SILENT and bool(
                deps.delivered or deps.reacted
            )  # a sticker/poll said it all
            if error is None and not silent and (output.strip() or not (deps.delivered or stopped)):
                try:
                    sent = await send_markdown(self.bot, target, output, trusted=False)
                except PartialDeliveryError as exc:  # what Telegram accepted still counts
                    error, sent = exc, exc.sent
                except TelegramAPIError as exc:
                    error, sent = exc, []
                deps.delivered.extend((m.message_id, "message") for m in sent)
                if sent:
                    async with self.db.transaction() as session:
                        await record_outgoing(session, sent)
            status = "failed" if error is not None else "cancelled" if stopped else "completed"
            async with self.db.transaction() as session:
                stored = await session.merge(run)
                for message_id, kind in deps.delivered:
                    record_output(session, stored, message_id, kind)
                for message_id in deps.reacted:
                    record_output(session, stored, message_id, "reaction")
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
            async with self.db.transaction() as session:
                charged = await self.funder.settle(
                    session, funds, run.id, delivered=bool(deps.delivered or deps.reacted)
                )
                await session.execute(
                    update(AgentRun).where(AgentRun.id == run.id).values(charged_credits=charged)
                )
            if stream.capped and not deps.delivered:
                await self._say(target, t(locale, "agent.capped"))
            if error is not None:
                record_run_failure(run.id, error, prompt)
                if not isinstance(error, TelegramAPIError) and not deps.delivered:
                    await self._apologize(target, locale, error, funds)
            return Outcome(run.id, status, tuple(message_id for message_id, _ in deps.delivered))
        except (
            BaseException
        ) as exc:  # died outside the model call (network, shutdown): never leave it running
            await self._abandon(run_id, funds, exc)
            raise
        finally:
            self.in_flight.discard(funds.operation_id)

    async def _abandon(self, run_id: int | None, funds: Funds, error: BaseException) -> None:
        """Mark a run that escaped its normal close as failed and return its hold (no-op once settled)."""
        with contextlib.suppress(Exception):
            async with self.db.transaction() as session:
                run = await session.get(AgentRun, run_id, with_for_update=True) if run_id else None
                if run is not None and run.status == "running":
                    run.status = "failed"
                    run.error_type = type(error).__name__
                    run.finished_at = datetime.now(UTC)
                if funds.reservation_id is not None:
                    await release(session, funds.operation_id)

    def stop(self, chat_id: int, draft_id: int) -> bool:
        """The user pressed stop on a draft; cancel the run that owns it."""
        token = self.active.get((chat_id, draft_id))
        if token is None:
            return False
        token.cancel()
        return True

    async def _apologize(self, target: Target, locale: str, error: BaseException, funds: Funds) -> None:
        if funds.funding is Funding.FREE and isinstance(error, ModelHTTPError) and error.status_code == 429:
            await self._say(target, t(locale, "agent.free_busy"))  # OpenRouter's shared free limits
            return
        await self._say(
            target, t(locale, "agent.timeout" if isinstance(error, TimeoutError) else "agent.failed")
        )

    async def _say(self, target: Target, text: str) -> None:
        try:
            await send_markdown(self.bot, target, text)
        except TelegramAPIError:
            log.info("notice not delivered")


class _Stream:
    """Feeds streamed events to progress, remembers the latest answer text and enforces the money cap.

    Before each tool call the run's responses so far are priced; once they reach the credits held
    for the run, the run is stopped like a user stop (whatever was written is posted).
    """

    def __init__(
        self, progress: Progress, token: CancellationToken, settings: Settings, funds: Funds, *, start: int
    ) -> None:
        self.progress = progress
        self.token = token
        self.settings = settings
        self.funds = funds
        self.start = start  # index of the run's first message in the context's history
        self.partial = ""
        self.capped = False

    def over_budget(self, messages: list[ModelMessage]) -> bool:
        if self.funds.reservation_id is None:
            return False
        spent = totals(messages[self.start :]).cost_usd
        return self.settings.credits_for_usd(float(spent)) >= self.funds.credits

    async def handle(self, ctx: RunContext[RunDeps], events: AsyncIterable[AgentStreamEvent]) -> None:
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
                if self.over_budget(ctx.messages):
                    log.info("run %s stopped at its money cap", ctx.deps.run_id)
                    self.capped = True
                    self.token.cancel()
                if ctx.deps.target.reply_to in ctx.deps.reacted:
                    self.progress.keep_reaction()  # Derp reacted to the trigger itself: don't overwrite it
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
        line = neutralize(f"{one_line(asker)}: {message.text or message.caption or ''}")
    parts: list[str] = []
    reply = message.reply_to_message
    if reply is not None and (reply.message_id not in window.ids):
        replied = await session.get(Message, (message.chat.id, reply.message_id))
        if replied is not None and replied.deleted_at is None:
            parts.append("Replying to: " + render_line((await load_views(session, [replied]))[0], tz))
        elif reply.text or reply.caption:
            author = reply.from_user.full_name if reply.from_user else "someone"
            parts.append(neutralize(f"Replying to {one_line(author)}: {reply.text or reply.caption}"))
    parts.append(f"<current_message>\n{line}\n</current_message>")
    return "\n".join(parts)
