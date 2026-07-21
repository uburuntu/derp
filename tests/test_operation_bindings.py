"""Operation request bindings are deterministic without exposing request text."""

from __future__ import annotations

import math

import pytest

from derp.execution import Feature
from derp.operations.bindings import (
    MAX_REQUEST_BINDING_PAYLOAD_BYTES,
    OperationRequestBinder,
)


def _binder() -> OperationRequestBinder:
    return OperationRequestBinder(b"request-binding-test-key".ljust(32, b"!"))


def test_binding_is_canonical_keyed_and_content_free() -> None:
    binder = _binder()
    private_prompt = "draw my private observatory"

    first = binder.bind(
        Feature.IMAGE_GENERATE,
        {"prompt": private_prompt, "style": "ink"},
    )
    reordered = binder.bind(
        Feature.IMAGE_GENERATE,
        {"style": "ink", "prompt": private_prompt},
    )
    another_key = OperationRequestBinder(b"another-binding-key".ljust(32, b"!")).bind(
        Feature.IMAGE_GENERATE,
        {"prompt": private_prompt, "style": "ink"},
    )

    assert first == reordered
    assert first != another_key
    assert len(first) == 64
    assert private_prompt not in first
    assert private_prompt not in repr(binder)


def test_binding_separates_features_and_request_values() -> None:
    binder = _binder()
    values = {"prompt": "same text"}

    assert binder.bind(Feature.IMAGE_GENERATE, values) != binder.bind(
        Feature.IMAGE_EDIT,
        values,
    )
    assert binder.bind(Feature.IMAGE_GENERATE, values) != binder.bind(
        Feature.IMAGE_GENERATE,
        {"prompt": "different text"},
    )


@pytest.mark.parametrize("key", [b"short", bytearray(b"x" * 32)])
def test_binding_requires_a_strong_immutable_key(key: object) -> None:
    with pytest.raises(TypeError if isinstance(key, bytearray) else ValueError):
        OperationRequestBinder(key)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"value": object()},
        {"value": math.inf},
        {"value": "x" * (MAX_REQUEST_BINDING_PAYLOAD_BYTES + 1)},
    ],
)
def test_binding_rejects_unbounded_or_non_json_values(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _binder().bind(Feature.CHAT, values)
