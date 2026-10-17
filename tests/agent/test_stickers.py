from datetime import UTC, datetime

import pytest

from derp.agent.tools.telegram import MOOD_OF, MOODS, VS16, pick_sticker, sticker_emojis
from derp.db.engine import Database
from derp.history.models import Chat, Message, MessageMedia, Sticker

BOT = 77
CHAT = -900
OTHER = -901


def test_moods_are_disjoint_and_without_selectors() -> None:
    emojis = [emoji for mood in MOODS for emoji in mood]
    assert len(emojis) == len(set(emojis)) == len(MOOD_OF)
    assert not any(VS16 in emoji for emoji in emojis)
    assert MOOD_OF["😂"] is MOOD_OF["🤣"] is MOOD_OF["😆"]


def sticker(unique: str, emoji: str, *, uses: int = 1, curated: bool = False) -> Sticker:
    return Sticker(
        bot_id=BOT, file_unique_id=unique, file_id=f"f-{unique}", emoji=emoji, uses=uses, curated=curated
    )


@pytest.fixture
async def stickers(database: Database) -> Database:
    """The global index, plus stickers members of CHAT (and another chat) posted."""
    async with database.transaction() as session:
        session.add_all(
            [Chat(id=CHAT, type="supergroup", title="S"), Chat(id=OTHER, type="supergroup", title="O")]
        )
        session.add_all(
            [
                sticker("rofl-global", "🤣", uses=50),
                sticker("joy-global", "😂", uses=3),
                sticker("heart", "❤️", uses=9),  # Telegram's tag carries the selector
                sticker("rofl-chat", "🤣"),
                sticker("grin-other", "😆", uses=1),
                sticker("cool", "😎", uses=2),
            ]
        )
        await session.flush()
        posted = [(CHAT, 1, "rofl-chat"), (CHAT, 2, "rofl-chat"), (CHAT, 3, "cool"), (OTHER, 1, "grin-other")]
        for chat_id, message_id, _ in posted:
            session.add(
                Message(
                    chat_id=chat_id,
                    message_id=message_id,
                    date=datetime.now(UTC),
                    kind="sticker",
                    payload={},
                    capture="ambient",
                )
            )
        await session.flush()
        for chat_id, message_id, unique in posted:
            session.add(
                MessageMedia(
                    chat_id=chat_id,
                    message_id=message_id,
                    type="sticker",
                    file_id=f"f-{unique}",
                    bot_id=BOT,
                    file_unique_id=unique,
                )
            )
    return database


@pytest.mark.db
async def test_the_exact_emoji_wins_then_its_mood_with_chat_favourites_first(stickers: Database) -> None:
    async with stickers.session() as session:
        assert await pick_sticker(session, CHAT, BOT, "😂") == "f-joy-global"  # exact beats the mood
        assert await pick_sticker(session, CHAT, BOT, "😹") == "f-rofl-chat"  # mood: this chat's own first
        assert await pick_sticker(session, OTHER, BOT, "😹") == "f-grin-other"  # …that chat's own
        assert await pick_sticker(session, -1, BOT, "😹") == "f-rofl-global"  # else the index, most used
        assert await pick_sticker(session, CHAT, BOT, "❤") == "f-heart"  # selectors don't matter
        assert await pick_sticker(session, CHAT, BOT, "😍") == "f-heart"
        assert await pick_sticker(session, CHAT, BOT, "👍🏽") is None  # nothing in that mood
        assert await pick_sticker(session, CHAT, BOT, "🦖") is None
        assert await pick_sticker(session, CHAT, BOT + 1, "😂") is None  # another bot's file ids


@pytest.mark.db
async def test_a_miss_lists_emojis_there_are_stickers_for(stickers: Database) -> None:
    async with stickers.session() as session:
        listed = await sticker_emojis(session, CHAT, BOT)
        assert listed[:2] == ["🤣", "😎"]  # this chat's, most used first
        assert set(listed) == {"🤣", "😎", "😂", "❤️", "😆"} and len(listed) == len(set(listed))
        assert await sticker_emojis(session, CHAT, BOT + 1) == []
