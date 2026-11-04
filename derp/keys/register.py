"""Plugging a key in and taking it out again: OpenRouter's side of a contributed key.

Plugging in makes the contributor an OpenRouter sub-key (its own spending on Derp's credits capped at
``sub_key_limit_usd`` a month), registers their provider key behind it for the provider's curated models
(and its image model) with shared capacity removed, and pings the cheapest of those models through it.
A key the provider rejects or that has no credits is taken out again at once. Derp keeps only the
sealed sub-key and OpenRouter's ids; the pasted key itself is never stored or logged.
"""

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import update

from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import OpenRouterAPI, OpenRouterError, OpenRouterManagement
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.routing import curated
from derp.keys.vault import seal

log = logging.getLogger("derp.keys")
# Provider API keys as people paste them: OpenAI (sk-, sk-proj-, sk-svcacct-…) and Anthropic (sk-ant-…).
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")
type Rejection = Literal["unknown", "rejected", "no_credits", "unavailable"]


def pasted_key(text: str | None) -> str | None:
    """A provider API key in a message (the whole of it, as pasted), or None."""
    found = KEY_PATTERN.search(text or "")
    return found.group(0) if found else None


def provider_of(key: str) -> str:
    return "anthropic" if key.startswith("sk-ant-") else "openai"


def allowed_models(settings: Settings, provider: str) -> list[str]:
    """What a provider's keys are registered for: its curated chat models and its image model."""
    models = [m.model for m in curated(settings, provider)]
    if image := settings.keys.images.get(provider):
        models.append(image)
    return models


@dataclass(frozen=True, slots=True)
class Refused:
    why: Rejection


async def plug_in(
    db: Database,
    settings: Settings,
    management: OpenRouterManagement,
    api: OpenRouterAPI,
    user_id: int,
    raw: str,
) -> ContributedKey | Refused:
    """Register ``raw`` for ``user_id``; it serves their private chat at once (other chats once turned on).

    Starts at the provider's recommended model and the middle monthly cap; the flow asks for both next.
    """
    provider = provider_of(raw)
    models = curated(settings, provider)
    if not models:
        return Refused("unknown")
    sub = await management.create_key(
        f"derp contributor {secrets.token_hex(4)}", limit_usd=settings.keys.sub_key_limit_usd
    )
    try:
        credential = await management.register(
            provider, raw, name="derp", sub_key_hash=sub.hash, models=allowed_models(settings, provider)
        )
    except OpenRouterError as error:
        await management.delete_key(sub.hash)
        return Refused("rejected" if error.status in (400, 401, 403, 422) else "unavailable")
    cheapest = min(models, key=lambda m: m.prompt + m.completion)
    try:
        await api.ping(cheapest.model, provider, api_key=sub.secret)
    except OpenRouterError as error:
        await _take_out(management, credential.id, sub.hash)
        why: Rejection = (
            "rejected"
            if error.status in (401, 403)
            else "no_credits"
            if error.status in (402, 429)
            else "unavailable"
        )
        log.info("contributed key refused: %s", error.status)
        return Refused(why)
    chosen = next((m for m in models if m.recommended), models[0])
    caps = settings.keys.caps_usd
    now = datetime.now(UTC)
    async with db.transaction() as session:
        key = ContributedKey(
            user_id=user_id,
            provider=provider,
            label=credential.label or f"sk-…{raw[-4:]}",
            model=chosen.model,
            cap_usd=Decimal(caps[len(caps) // 2]),
            cycle_anchor=now.date(),
            sub_key_hash=sub.hash,
            sub_key=seal(settings.keys_secret, sub.secret),
            credential_id=credential.id,
        )
        session.add(key)
        await session.flush()
        session.add(KeyChat(key_id=key.id, chat_id=user_id, state="on", enabled_by=user_id, enabled_at=now))
    return key


async def take_out(db: Database, management: OpenRouterManagement, key: ContributedKey) -> None:
    """Unregister a key at OpenRouter and unplug it everywhere; the row stays for the ledger."""
    await _take_out(management, key.credential_id, key.sub_key_hash)
    now = datetime.now(UTC)
    async with db.transaction() as session:
        row = await session.get(ContributedKey, key.id, with_for_update=True)
        if row is not None:
            row.removed_at = now
        await session.execute(
            update(KeyChat).where(KeyChat.key_id == key.id).values(state="off", off_noticed_at=now)
        )


async def _take_out(management: OpenRouterManagement, credential_id: str, sub_key_hash: str) -> None:
    for remove, target in ((management.unregister, credential_id), (management.delete_key, sub_key_hash)):
        try:
            await remove(target)
        except OpenRouterError as error:
            if error.status != 404:  # already gone is gone
                raise


async def allow_models(settings: Settings, management: OpenRouterManagement, key: ContributedKey) -> None:
    """Keep a key registered for today's curated models (a pick from a list that changed since)."""
    await management.allow_models(key.credential_id, allowed_models(settings, key.provider))
