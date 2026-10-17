import pytest

from derp.db.engine import Database
from derp.memory.models import Note
from derp.memory.notes import NOTES_POLICY, USER_NOTES_CAP, Memory, add_note, load_memory, notes_block


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
    assert "Chat notes:\n- note " in block and "Notes about Ann:" in block and "other chat" not in block


@pytest.mark.parametrize(
    ("mode", "says"),
    [
        ("auto", "on your own"),
        ("members", "only when someone explicitly asks"),
        ("admins", "goes to the admins for approval"),
        ("off", "don't save any"),
    ],
)
def test_the_notes_part_states_the_chat_notes_policy(mode: str, says: str) -> None:
    text = notes_block(Memory([], []), "Ann", mode)
    assert text == NOTES_POLICY[mode]  # exactly one line, one mode, even without notes
    assert text is not None and says in text


def test_runs_without_memory_tools_get_no_notes_policy() -> None:
    note = Note(id=42, chat_id=-5, user_id=None, text="pizza on fridays", author_id=1, state="active")
    text = notes_block(Memory([note], []), "Ann")
    assert text is not None and not any(policy in text for policy in NOTES_POLICY.values())
    assert "- note 42: pizza on fridays" in text  # never "#42": that would be a message reference
