from datetime import UTC, datetime

import pytest

from derp.agent.persona import NOTES_POLICY, situation
from derp.history.models import Chat

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def brief(mode: str, *, notes_policy: bool = True) -> str:
    chat = Chat(id=-5, type="supergroup", title="Club", notes_mode=mode, profile={})
    return situation(chat=chat, asker="Ann", now=NOW, tz=UTC, notes_policy=notes_policy)


@pytest.mark.parametrize(
    ("mode", "says"),
    [
        ("auto", "on your own"),
        ("members", "only when someone explicitly asks"),
        ("admins", "goes to the admins for approval"),
        ("off", "don't save any"),
    ],
)
def test_the_situation_states_the_chat_notes_policy(mode: str, says: str) -> None:
    text = brief(mode)
    assert NOTES_POLICY[mode] in text and says in text
    assert sum(policy in text for policy in NOTES_POLICY.values()) == 1  # exactly one line, one mode


def test_runs_without_memory_tools_get_no_notes_policy() -> None:
    assert not any(policy in brief("auto", notes_policy=False) for policy in NOTES_POLICY.values())
