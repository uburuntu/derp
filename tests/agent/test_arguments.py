from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from derp.agent.arguments import repair_object


class Item(BaseModel):
    question: str
    correct: int | None = None


class Query(BaseModel):
    items: list[Item]
    kinds: list[str] = []
    note: str = ""
    since: datetime | None = None
    media: Literal["photo", "video"] | None = None
    days: int = 7


SCHEMA = Query.model_json_schema()


def fix(args: dict[str, object]) -> dict[str, object]:
    return repair_object(args, SCHEMA, SCHEMA.get("$defs", {}))


def test_quoted_arrays_and_objects_are_parsed() -> None:
    fixed = fix({"items": '[{"question": "2+2?", "correct": "1"}]', "kinds": '["text"]'})
    assert Query.model_validate(fixed) == Query(items=[Item(question="2+2?", correct=1)], kinds=["text"])


def test_empty_optionals_mean_not_given() -> None:
    fixed = fix({"items": "[]", "since": "", "media": "", "note": "", "days": "30"})
    assert fixed == {"items": [], "days": 30}


def test_strings_stay_strings_and_good_arguments_pass_through() -> None:
    args = {"items": [{"question": "[1, 2]"}], "note": '["not a list"]'}
    assert fix(args) == args
