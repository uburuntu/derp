from datetime import UTC, datetime

from aiogram.types import Update

from derp.ops.observability import update_attributes
from derp.ops.redaction import MASK, Redactor


def test_redactor_masks_known_and_shaped_secrets() -> None:
    redact = Redactor(["sk-or-v1-supersecretvalue", "postgresql://u:p4ssw0rd@db/x"])
    line = (
        "key=sk-or-v1-supersecretvalue url=https://api.telegram.org/file/bot123456789:"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/x.jpg dsn=postgresql://u:p4ssw0rd@db/x"
    )
    out = redact(line)
    assert "supersecret" not in out
    assert "AAAAAAAA" not in out
    assert "p4ssw0rd" not in out
    assert MASK in out


def test_redactor_json_keeps_ids() -> None:
    redact = Redactor()
    assert redact.json({"chat_id": -100, "api_key": "x", "nested": [{"token": 1}]}) == {
        "chat_id": -100,
        "api_key": MASK,
        "nested": [{"token": MASK}],
    }


def test_update_attributes_are_numeric_only() -> None:
    update = Update.model_validate(
        {
            "update_id": 7,
            "message": {
                "message_id": 42,
                "date": int(datetime.now(UTC).timestamp()) - 20,
                "message_thread_id": 3,
                "chat": {"id": -1001, "type": "supergroup", "title": "secret title"},
                "from": {"id": 5, "is_bot": False, "first_name": "Ann"},
                "text": "private words",
            },
        }
    )
    attributes = update_attributes(update)
    assert 20 <= attributes.pop("age_s") <= 22  # how late it arrived: a delivery stall shows here
    assert attributes == {
        "update_id": 7,
        "update_type": "message",
        "chat_id": -1001,
        "chat_type": "supergroup",
        "user_id": 5,
        "message_id": 42,
        "thread_id": 3,
    }
