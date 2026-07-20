"""Approved shared facts have a stable, explicitly untrusted wire form."""

from uuid import UUID

from derp.history.facts import ApprovedFact, render_approved_facts


def test_renderer_is_canonical_and_instruction_safe() -> None:
    rendered = render_approved_facts(
        [
            ApprovedFact(UUID(int=1), "The project is called Derp."),
            ApprovedFact(UUID(int=2), "Use Python for examples."),
        ]
    )

    assert rendered == (
        '{"facts":['
        '{"id":"00000000-0000-0000-0000-000000000001",'
        '"text":"The project is called Derp."},'
        '{"id":"00000000-0000-0000-0000-000000000002",'
        '"text":"Use Python for examples."}],'
        '"type":"untrusted_approved_shared_facts"}'
    )
    assert "instructions" not in rendered


def test_empty_fact_collection_still_has_typed_shape() -> None:
    assert render_approved_facts([]) == (
        '{"facts":[],"type":"untrusted_approved_shared_facts"}'
    )
