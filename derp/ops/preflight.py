"""Read-only production checks run by the deploy before cutover.

Never polls, sends messages or migrates. Exit code 0 means the new release
can start: configuration is complete, the database answers, and the bot's
Telegram-side settings match what Derp relies on.
"""

import asyncio
import shutil

from derp.config import get_settings
from derp.db.engine import Database
from derp.telegram.bot import create_bot


async def check() -> list[str]:
    settings = get_settings()
    settings.validate_core()
    problems: list[str] = []
    db = Database(settings.database_url, pool_size=1)
    bot = create_bot(settings)
    try:
        await asyncio.wait_for(db.check(), 15)
        me = await asyncio.wait_for(bot.get_me(), 15)
        if not me.can_read_all_group_messages:
            problems.append("BotFather privacy mode is on: ambient group context cannot work")
        if me.has_topics_enabled:
            problems.append("private-chat topics are enabled: Telegram takes a fee on every Stars purchase")
    finally:
        await bot.session.close()
        await db.close()
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg is missing")
    return problems


def main() -> None:
    try:
        problems = asyncio.run(check())
    except Exception as error:  # noqa: BLE001 - report the failure class, never secrets
        raise SystemExit(f"preflight failed: {type(error).__name__}") from None
    if problems:
        raise SystemExit("preflight failed: " + "; ".join(problems))
    print("preflight passed")  # noqa: T201


if __name__ == "__main__":
    main()
