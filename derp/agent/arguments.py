"""Tool arguments as some models send them: everything quoted.

GLM serializes every argument as a string: arrays and objects as JSON text,
numbers as "30", unused optional arguments as "". It also fills every optional
argument with a placeholder (0 under a minimum of 1, [] for a single choice) and
sends one item where a list is wanted. Pydantic coerces numbers and booleans but
rejects the rest, the model repeats the mistake on its retry, and the run fails.
``Lenient`` repairs arguments against each tool's JSON schema before the tool's
own validator sees them: JSON text where the schema wants an array or object is
parsed, a lone item where it wants an array becomes a list of one, and "" or a
placeholder the schema rejects ([], {}, 0, null) for an optional argument means
"not given". Arguments that already fit pass through unchanged.

The run's other repair layer, Harness ``RepairToolArguments`` (``tools.CAPABILITIES``),
does a different job one step earlier: its ``before_tool_validate`` hook fixes
malformed JSON *text* (unclosed braces, trailing commas) and leaves valid JSON
alone. ``Lenient`` then fixes *valid* JSON whose values are quoted against the
schema, which no JSON repair can see. Both are needed; neither covers the other.
"""

import contextlib
import json
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from pydantic_ai import RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import SchemaValidatorProt, ToolsetTool
from pydantic_core import SchemaValidator

from derp.agent.deps import RunDeps

type Schema = dict[str, Any]
type Partial = bool | Literal["off", "on", "trailing-strings"]


def _resolve(schema: Schema, defs: Schema) -> Schema:
    while isinstance(ref := schema.get("$ref"), str) and ref.startswith("#/$defs/"):
        schema = defs.get(ref.removeprefix("#/$defs/"), {})
    return schema


def _branches(schema: Schema, defs: Schema) -> list[Schema]:
    schema = _resolve(schema, defs)
    options = schema.get("anyOf") or schema.get("oneOf")
    return [b for option in options for b in _branches(option, defs)] if options else [schema]


def _branch(schema: Schema, defs: Schema, kind: str) -> Schema | None:
    return next((b for b in _branches(schema, defs) if b.get("type") == kind), None)


def _json_types(value: Any) -> set[str]:
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"}
    return {"string"} if isinstance(value, str) else {"object"} if isinstance(value, dict) else set()


def _kinds(schema: Schema) -> set[str]:
    kind = schema.get("type")
    return (
        set(cast(list[str], kind)) if isinstance(kind, list) else {kind} if isinstance(kind, str) else set()
    )


def _accepts(schema: Schema, value: Any) -> bool:
    """Whether one schema branch takes a placeholder ([], {}, 0 or null) as it is."""
    kinds = _kinds(schema)
    if not kinds:
        return "enum" not in schema and "const" not in schema
    if value == [] or value == {}:
        return ("array" if value == [] else "object") in kinds and not schema.get("minItems")
    if value is None:
        return "null" in kinds
    return (
        bool(kinds & _json_types(value))
        and schema.get("minimum", 0) <= 0
        and schema.get("exclusiveMinimum", -1) < 0
        and schema.get("maximum", 0) >= 0
        and value in schema.get("enum", [value])
    )


def _unset(value: Any, schema: Schema, defs: Schema) -> bool:
    """An optional argument the model filled only to fill it: a placeholder its schema rejects."""
    placeholder = value is None or value == [] or value == {} or (type(value) is int and value == 0)
    return placeholder and not any(_accepts(b, value) for b in _branches(schema, defs))


def repair(value: Any, schema: Schema, defs: Schema) -> Any:
    """``value`` made to fit ``schema`` where a model quoted it or sent one item for a list; else unchanged."""
    if isinstance(value, str) and not _branch(schema, defs, "string"):
        with contextlib.suppress(ValueError):
            value = json.loads(value)
    if (
        value is not None
        and not isinstance(value, list)
        and _branch(schema, defs, "array")
        and not any(_kinds(b) & _json_types(value) for b in _branches(schema, defs))
    ):
        value = [value]
    if isinstance(value, list) and (array := _branch(schema, defs, "array")):
        items: Schema = array.get("items", {})
        return [repair(item, items, defs) for item in cast(list[Any], value)]
    if isinstance(value, dict) and (shape := _branch(schema, defs, "object")):
        return repair_object(cast(dict[str, Any], value), shape, defs)
    return cast(Any, value)


def repair_object(args: dict[str, Any], schema: Schema, defs: Schema) -> dict[str, Any]:
    properties: Schema = schema.get("properties", {})
    required = set(schema.get("required", ()))
    repaired: dict[str, Any] = {}
    for name, value in args.items():
        if name not in properties or name in required:
            repaired[name] = repair(value, properties[name], defs) if name in properties else value
        elif value != "" and not _unset(
            fixed := repair(value, properties[name], defs), properties[name], defs
        ):
            repaired[name] = fixed
    return repaired


@dataclass(frozen=True, slots=True)
class _Repairing:
    inner: SchemaValidator | SchemaValidatorProt
    schema: Schema

    # ``input`` is the protocol's parameter name (SchemaValidatorProt).
    def validate_json(
        self,
        input: str | bytes | bytearray,  # noqa: A002
        *,
        allow_partial: Partial = False,
        **kwargs: Any,
    ) -> Any:
        try:
            args = json.loads(input or "{}")
        except ValueError:  # partial JSON while streaming, or not JSON at all: the tool's own rules
            return self.inner.validate_json(input, allow_partial=allow_partial, **kwargs)
        if not isinstance(args, dict):
            return self.inner.validate_json(input, allow_partial=allow_partial, **kwargs)
        repaired = json.dumps(
            repair_object(cast(dict[str, Any], args), self.schema, self.schema.get("$defs", {}))
        )
        return self.inner.validate_json(repaired, allow_partial=allow_partial, **kwargs)

    def validate_python(self, input: Any, *, allow_partial: Partial = False, **kwargs: Any) -> Any:  # noqa: A002
        args = (
            repair_object(cast(dict[str, Any], input), self.schema, self.schema.get("$defs", {}))
            if isinstance(input, dict)
            else input
        )
        return self.inner.validate_python(args, allow_partial=allow_partial, **kwargs)


@dataclass
class Lenient(WrapperToolset[RunDeps]):
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        tools = await super().get_tools(ctx)
        return {
            name: replace(
                tool, args_validator=_Repairing(tool.args_validator, tool.tool_def.parameters_json_schema)
            )
            for name, tool in tools.items()
        }
