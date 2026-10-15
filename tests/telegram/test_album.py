from typing import Any

from aiogram.types import InputMediaPhoto

from derp.telegram.sender import Target, send_album, send_markdown


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def send_media_group(self, **kwargs: Any) -> list[str]:
        self.calls.append(("group", len(kwargs["media"])))
        return ["m"] * len(kwargs["media"])

    async def send_photo(self, **kwargs: Any) -> str:
        self.calls.append(("photo", kwargs["photo"]))
        return "m"

    async def send_rich_message(self, **kwargs: Any) -> str:
        self.calls.append(("rich", kwargs["rich_message"].markdown))
        return "m"


async def test_album_batches_are_balanced_and_singles_go_alone() -> None:
    bot: Any = Recorder()
    photos = [InputMediaPhoto(media=f"id{i}") for i in range(11)]
    assert len(await send_album(bot, Target(1), photos)) == 11
    assert bot.calls == [("group", 6), ("group", 5)]
    bot.calls.clear()
    await send_album(bot, Target(1), photos[:1])
    assert bot.calls == [("photo", "id0")]


async def test_blank_markdown_degrades_to_ellipsis() -> None:
    bot: Any = Recorder()
    await send_markdown(bot, Target(1), " \n ")
    assert bot.calls == [("rich", "…")]
