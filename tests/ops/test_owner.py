import logging
from typing import Any

from derp.config import Settings
from derp.ops.owner import ErrorTally, send_error_digest


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))


async def test_errors_are_counted_by_logger_and_sent_once(settings: Settings) -> None:
    tally = ErrorTally()
    logger = logging.getLogger("derp.test-tally")
    logger.addHandler(tally)
    try:
        logger.error("secret user text %s", "leaks?")
        logger.error("again")
        logger.warning("not counted")
    finally:
        logger.removeHandler(tally)
    bot = FakeBot()
    configured = settings.model_copy(
        update={"ops_chat_id": -100, "logfire_url": "https://logfire.example/derp"}
    )
    await send_error_digest(bot, configured, tally, 10)  # type: ignore[arg-type]
    await send_error_digest(bot, configured, tally, 10)  # type: ignore[arg-type]
    assert bot.sent == [
        (-100, "⚠️ 2 error(s) in the last 10 min: derp.test-tally ×2\nhttps://logfire.example/derp")
    ]
