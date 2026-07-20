"""Capability tokens are opaque, domain-separated, and restart-stable."""

from uuid import uuid4

import pytest

from derp.approvals import ApprovalTokenCodec
from derp.delivery import ResendTokenCodec
from derp.security import CapabilityTokenCodec


def test_capability_codec_is_stable_verifiable_and_domain_separated() -> None:
    subject_id = uuid4()
    secret = b"shared-test-secret".ljust(32, b"!")
    first = CapabilityTokenCodec(secret, context=b"first")
    second = CapabilityTokenCodec(secret, context=b"second")

    token = first.issue(subject_id)

    assert len(token) == 43
    assert token == first.issue(subject_id)
    assert first.verify(subject_id, token)
    assert not first.verify(uuid4(), token)
    assert not first.verify(subject_id, f"{token[:-1]}x")
    assert second.issue(subject_id) != token
    assert len(first.digest(token)) == 64


def test_approval_and_resend_capabilities_cannot_cross_domains() -> None:
    subject_id = uuid4()
    secret = b"shared-test-secret".ljust(32, b"!")

    approval = ApprovalTokenCodec(secret).issue(subject_id)
    resend = ResendTokenCodec(secret).issue(subject_id)

    assert approval != resend
    assert not ApprovalTokenCodec(secret).verify(subject_id, resend)


@pytest.mark.parametrize(
    ("secret", "context", "message"),
    [
        (b"short", b"valid", "at least 32 bytes"),
        (b"x" * 32, b"", "must not be empty"),
        (b"x" * 32, b"x" * 256, "too long"),
    ],
)
def test_capability_codec_rejects_weak_or_ambiguous_configuration(
    secret: bytes,
    context: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CapabilityTokenCodec(secret, context=context)
