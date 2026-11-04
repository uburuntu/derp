"""Put secrets into the local ``.env`` without them passing through a chat, a shell history or a log.

    uv run python tools/env_secrets.py                      # the contributed-keys secrets
    uv run python tools/env_secrets.py OPENAI_TEST_KEY ...  # any names

Each value is read hidden (getpass); an empty answer keeps the current one. ``KEYS_SECRET`` (the key that
encrypts contributors' OpenRouter keys at rest) can be generated instead of typed. Values are never echoed:
only names and whether each was set, kept or generated.
"""

import base64
import getpass
import re
import secrets
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_NAMES = ("OPENROUTER_MANAGEMENT_KEY", "KEYS_SECRET", "OPENAI_TEST_KEY", "ANTHROPIC_TEST_KEY")
GENERATED = {"KEYS_SECRET"}  # 32 random bytes, urlsafe base64: offered when left empty


def upsert(text: str, name: str, value: str) -> str:
    line = f"{name}={value}"
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text)
    return text.rstrip("\n") + f"\n{line}\n"


def current(text: str, name: str) -> bool:
    return bool(re.search(rf"^{re.escape(name)}=.+$", text, re.MULTILINE))


def main(names: list[str]) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    for name in names:
        has = current(text, name)
        hint = " (set; Enter keeps it)" if has else (" (Enter generates one)" if name in GENERATED else "")
        value = getpass.getpass(f"{name}{hint}: ").strip()
        if not value and name in GENERATED and not has:
            value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            print(f"  {name}: generated")
        elif not value:
            print(f"  {name}: {'kept' if has else 'skipped'}")
            continue
        else:
            print(f"  {name}: set")
        text = upsert(text, name, value)
    ENV.write_text(text)
    ENV.chmod(0o600)


if __name__ == "__main__":
    main(sys.argv[1:] or list(DEFAULT_NAMES))
