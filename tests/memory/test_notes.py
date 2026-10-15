import pytest

from derp.db.engine import Database
from derp.memory.notes import USER_NOTES_CAP, add_note, load_memory, notes_block


@pytest.mark.db
async def test_notes_are_validated_capped_and_scoped(database: Database) -> None:
    async with database.transaction() as session:
        assert await add_note(session, -1, "   ", user_id=None, author_id=1) == "empty note"
        assert str(await add_note(session, -1, "x" * 301, user_id=None, author_id=1)).startswith("too long")
        for index in range(USER_NOTES_CAP):
            await add_note(session, -1, f"fact {index}", user_id=7, author_id=7)
        assert str(await add_note(session, -1, "one more", user_id=7, author_id=7)).startswith("full")
        await add_note(session, -1, "group   loves   pizza", user_id=None, author_id=7)
        await add_note(session, -1, "pending idea", user_id=None, author_id=8, state="proposed")
        await add_note(session, -2, "other chat", user_id=None, author_id=7)
    async with database.session() as session:
        memory = await load_memory(session, -1, 7)
        assert [n.text for n in memory.chat_notes] == ["group loves pizza"]
        assert len(memory.my_notes) == USER_NOTES_CAP
        other = await load_memory(session, -1, 8, include_proposed=True)
        assert [n.state for n in other.chat_notes] == ["proposed", "active"] and other.my_notes == []
    block = notes_block(memory, "Ann")
    assert block is not None and block.startswith("<notes")
    assert "Chat notes:\n- #" in block and "Notes about Ann:" in block and "other chat" not in block
