"""Resend capabilities remain opaque, stable, and callback-safe."""

from uuid import uuid4

import pytest

from derp.delivery import ResendTokenCodec


def test_resend_token_is_stable_opaque_and_database_digestible() -> None:
    intent_id = uuid4()
    codec = ResendTokenCodec(b"a" * 32)

    token = codec.issue(intent_id)

    assert token == codec.issue(intent_id)
    assert token != str(intent_id)
    assert len(token) == 43
    assert len(codec.digest(token)) == 64
    assert codec.issue(uuid4()) != token
    assert ResendTokenCodec(b"b" * 32).issue(intent_id) != token


def test_resend_token_codec_requires_high_entropy_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ResendTokenCodec(b"short")
