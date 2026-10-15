from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetChat, GetUpdates, SendMessage

from derp.telegram.heartbeat import ready
from derp.telegram.policy import TelegramRequestPolicy


class Script:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, bot: Bot, method: Any) -> Any:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def policy(tmp_path: Path | None = None) -> TelegramRequestPolicy:
    async def no_sleep(_: float) -> None:
        return None

    return TelegramRequestPolicy(sleep=no_sleep, heartbeat=tmp_path / "poll" if tmp_path else None)


BOT: Any = object()


async def test_retry_after_is_retried_for_sends() -> None:
    send = SendMessage(chat_id=1, text="hi")
    script = Script(TelegramRetryAfter(method=send, message="flood", retry_after=1), "ok")
    assert await policy()(script, BOT, send) == "ok"
    assert script.calls == 2


async def test_long_retry_after_is_not_waited() -> None:
    send = SendMessage(chat_id=1, text="hi")
    script = Script(TelegramRetryAfter(method=send, message="flood", retry_after=600))
    with pytest.raises(TelegramRetryAfter):
        await policy()(script, BOT, send)


async def test_ambiguous_send_is_never_replayed() -> None:
    send = SendMessage(chat_id=1, text="hi")
    script = Script(TelegramNetworkError(method=send, message="reset"), "dup")
    with pytest.raises(TelegramNetworkError):
        await policy()(script, BOT, send)
    assert script.calls == 1


async def test_reads_retry_network_errors() -> None:
    read = GetChat(chat_id=1)
    script = Script(TelegramNetworkError(method=read, message="reset"), "chat")
    assert await policy()(script, BOT, read) == "chat"


async def test_successful_poll_marks_heartbeat(tmp_path: Path) -> None:
    await policy(tmp_path)(Script([]), BOT, GetUpdates())
    assert ready(tmp_path / "poll")
    assert not ready(tmp_path / "missing")
