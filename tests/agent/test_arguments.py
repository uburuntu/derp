from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

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


def test_placeholders_the_schema_rejects_mean_not_given() -> None:
    from derp.history.queries import ChatQuery

    schema = ChatQuery.model_json_schema()
    sent = {  # how GLM fills a query it means as "everything": every optional set to something
        "kinds": "text", "users": [], "days": 0, "since": None, "min_reactions": 0,
        "group_by": [], "from_derp": False, "whole_chat": False, "limit": 20, "contains": "",
    }  # fmt: skip
    fixed = repair_object(sent, schema, schema.get("$defs", {}))
    assert fixed == {
        "kinds": ["text"], "users": [], "since": None, "from_derp": False, "whole_chat": False, "limit": 20,
    }  # fmt: skip
    ChatQuery.model_validate(fixed)


def test_a_lone_item_becomes_a_list_and_accepted_zeroes_stay() -> None:
    assert fix({"items": '{"question": "q"}', "kinds": "text", "days": 0}) == {
        "items": [{"question": "q"}],
        "kinds": ["text"],
        "days": 0,
    }


class Slips(BaseModel):
    group_by: Literal["user", "day"] | None = None
    since: datetime | None = None
    correct: Annotated[int, Field(ge=0)] | None = None
    reply_to: Annotated[int, Field(ge=1)] | None = None
    kind: Literal["reminder", "prompt"] | None = None
    ids: list[int] | None = None
    kinds: list[Literal["text", "photo"]] | None = None
    names: list[str] | None = None
    shift: int = 0


SLIPS = Slips.model_json_schema()


def slip(args: dict[str, object]) -> dict[str, object]:
    return repair_object(args, SLIPS, SLIPS.get("$defs", {}))


def test_placeholder_text_and_sentinels_mean_not_given() -> None:
    sent = {"group_by": "[]", "since": "null", "correct": -1, "reply_to": 0, "kind": False, "shift": -1}
    assert slip(sent) == {"shift": -1}  # -1 where the schema takes it is a value


def test_enum_slips_and_joined_lists_are_read() -> None:
    fixed = slip({"group_by": "Users", "ids": "971098, 971099", "kinds": "text,photo", "names": "Ann, Bo"})
    assert fixed == {
        "group_by": "user",
        "ids": [971098, 971099],
        "kinds": ["text", "photo"],
        "names": ["Ann, Bo"],
    }
    Slips.model_validate(fixed)
