"""Provider API keys people paste into chats: found, and kept out of everything Derp stores or sends a model.

``history.middleware`` drops a message carrying one (no storage, no invocation) and ``keys.flows`` deletes
it; ``redact`` covers every other way a key's text can reach a row or a prompt (a reply to such a message,
a quote of it), through ``history.capture.message_values``, the one Telegram → row mapping.
"""

import re

# Provider API keys as people paste them: OpenAI (sk-, sk-proj-, sk-svcacct-…) and Anthropic (sk-ant-…).
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")
REDACTED = "[API key removed]"


def pasted_key(text: str | None) -> str | None:
    """A provider API key in a message (the whole of it, as pasted), or None."""
    found = KEY_PATTERN.search(text or "")
    return found.group(0) if found else None


def redact(text: str | None) -> str | None:
    return KEY_PATTERN.sub(REDACTED, text) if text else text
