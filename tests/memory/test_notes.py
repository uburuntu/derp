import pytest
from sqlalchemy import select

from derp.db.engine import Database
from derp.memory.models import Note
from derp.memory.notes import (
    CHAT_NOTES_CAP,
    MY_SHARE,
    NOTES_POLICY,
    PROMPT_CHARS,
    USER_NOTES_CAP,
    Memory,
    add_note,
    budgeted,
    decide,
    load_memory,
    matching,
    notes_block,
    rewrite,
)


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


def _note(note_id: int, text: str, user_id: int | None = None) -> Note:
    return Note(id=note_id, chat_id=-5, user_id=user_id, text=text, author_id=1, state="active")


def test_a_personal_note_survives_a_full_chat_notes_budget() -> None:
    chat = [_note(100 + i, "c" * 290) for i in range(CHAT_NOTES_CAP)]
    memory = Memory(chat, [_note(7, "Ann is allergic to nuts", user_id=1)])
    block = notes_block(memory, "Ann", "auto")
    assert block is not None and "- note 7: Ann is allergic to nuts" in block
    assert "older not shown; find_notes searches them):" in block  # the chat notes that didn't fit


def test_an_unused_share_goes_to_the_other_kind() -> None:
    lines = [f"- note {i}: " + "x" * 290 for i in range(20)]
    chat, mine = budgeted(lines, [])
    assert mine == [] and sum(map(len, chat)) > PROMPT_CHARS - 300  # chat notes fill the whole budget
    chat, mine = budgeted([], lines)
    assert chat == [] and sum(map(len, mine)) > PROMPT_CHARS - 300  # and so do the asker's own
    chat, mine = budgeted(lines, lines)
    assert sum(map(len, mine)) <= MY_SHARE < sum(map(len, mine)) + len(lines[0])  # each kind its share
    assert mine == lines[: len(mine)] and chat == lines[: len(chat)]  # newest first, no gaps


def test_left_out_notes_are_counted_and_searchable_only_with_the_tools() -> None:
    memory = Memory([_note(100 + i, "c" * 288) for i in range(15)], [])  # 300-char lines: 10 fit
    with_tools, without = notes_block(memory, "Ann", "members"), notes_block(memory, "Ann")
    assert with_tools is not None and without is not None
    assert "Chat notes (5 older not shown; find_notes searches them):" in with_tools
    assert "Chat notes (5 older not shown):" in without
    assert "older not shown" not in str(notes_block(Memory(memory.chat_notes[:3], []), "Ann"))


def test_matching_ranks_by_words_found_then_newest() -> None:
    memory = Memory(
        [
            _note(1, "Films to watch: Alien, Heat"),
            _note(2, "Pizza night is Friday"),
            _note(3, "Heat is overrated"),
        ],
        [_note(4, "Ann's favourite film: Heat", user_id=1)],
    )
    assert [n.id for n in matching(memory, "film heat")] == [4, 1, 3]
    assert [n.id for n in matching(memory, "PIZZA")] == [2]
    assert [n.id for n in matching(memory, "")] == [4, 3, 2, 1]
    assert matching(memory, "sushi") == []


@pytest.mark.db
async def test_an_edit_rewrites_in_place_or_waits_as_one_proposal(database: Database) -> None:
    async with database.transaction() as session:
        note = await add_note(session, -1, "meetups are on Fridays", user_id=None, author_id=7)
        assert isinstance(note, Note)
        assert await rewrite(session, note, "  ", propose=False, author_id=7) == "empty note"
        assert await rewrite(session, note, "meetups are on   Thursdays", propose=False, author_id=7) is note
        first = await rewrite(session, note, "meetups are on Mondays", propose=True, author_id=8)
        second = await rewrite(session, note, "meetups are on Sundays", propose=True, author_id=9)
        assert isinstance(first, Note) and first is second  # one pending edit per note, the latest text
        assert (second.state, second.replaces_id, second.author_id) == ("proposed", note.id, 9)
        note_id = note.id
    async with database.session() as session:
        memory = await load_memory(session, -1, 7, include_proposed=True)
        assert [(n.text, n.state) for n in memory.chat_notes] == [
            ("meetups are on Sundays", "proposed"),
            ("meetups are on Thursdays", "active"),
        ]
    async with database.transaction() as session:
        proposal = await session.scalar(select(Note).where(Note.replaces_id == note_id))
        assert proposal is not None
        await decide(session, proposal, approve=True)
    async with database.session() as session:
        rows = list(await session.scalars(select(Note)))
        assert [(n.id, n.text, n.state) for n in rows] == [(note_id, "meetups are on Sundays", "active")]


@pytest.mark.db
async def test_rejected_or_orphaned_edits_go_and_edits_dont_fill_the_cap(database: Database) -> None:
    async with database.transaction() as session:
        notes = [await add_note(session, -1, f"fact {i}", user_id=None, author_id=7) for i in range(2)]
        kept, dropped = (n for n in notes if isinstance(n, Note))
        rejected = await rewrite(session, kept, "fact zero, revised", propose=True, author_id=8)
        await rewrite(session, dropped, "fact one, revised", propose=True, author_id=8)
        assert isinstance(rejected, Note)
        await decide(session, rejected, approve=False)
        await session.delete(dropped)  # its pending edit goes with it
    async with database.session() as session:
        assert [n.text for n in await session.scalars(select(Note))] == ["fact 0"]
    async with database.transaction() as session:
        target = await session.scalar(select(Note))
        assert target is not None
        await rewrite(session, target, "fact 0, again", propose=True, author_id=8)
        for index in range(CHAT_NOTES_CAP - 1):
            assert isinstance(await add_note(session, -1, f"more {index}", user_id=None, author_id=7), Note)
        assert str(await add_note(session, -1, "one too many", user_id=None, author_id=7)).startswith("full")
