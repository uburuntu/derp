"""Contributors' OpenRouter sub-keys at rest: AES-GCM under ``KEYS_SECRET``.

A database dump alone never yields a key that can spend someone's provider credits. Any string works as
the secret (it is hashed to 32 bytes); ``tools/env_secrets.py`` generates a random one.
"""

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE = 12


def _cipher(secret: str) -> AESGCM:
    if not secret:
        raise ValueError("KEYS_SECRET is not set")
    return AESGCM(hashlib.sha256(secret.encode()).digest())


def seal(secret: str, plain: str) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + _cipher(secret).encrypt(nonce, plain.encode(), b"derp-sub-key")


def unseal(secret: str, sealed: bytes) -> str:
    return _cipher(secret).decrypt(sealed[:NONCE], sealed[NONCE:], b"derp-sub-key").decode()
