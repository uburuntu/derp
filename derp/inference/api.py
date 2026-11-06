"""Small OpenRouter REST client for what Pydantic AI does not cover:
image generation, speech-to-text, text-to-speech, decisions, the model catalog and generation lookups;
and the management API for contributed keys (``OpenRouterManagement``).
"""

import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import httpx

from derp.config import Settings

type Json = dict[str, Any]


REFUSAL_CODES = frozenset({"moderation_blocked", "content_policy_violation", "content_filter"})
_CODE = re.compile(r"[a-z][a-z0-9_]{2,47}")


class OpenRouterError(Exception):
    """``code`` is a short machine code (``moderation_blocked``) safe to log, never the message; ``provider``
    names the host that returned the error when OpenRouter passed one on."""

    def __init__(
        self, status: int, message: str, *, code: str | None = None, provider: str | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.provider = provider
        self.refused = code in REFUSAL_CODES or (
            status == 403 and any(word in message.lower() for word in ("moderation", "flagged"))
        )
        super().__init__(f"OpenRouter {status}: {message}")

    @property
    def retryable(self) -> bool:
        return self.status in {408, 429, 500, 502, 503, 504, 529}


def _error_code(error: Mapping[str, Any]) -> str | None:
    """The provider's own error code when OpenRouter passed its raw body on, else OpenRouter's (when a word)."""
    metadata = error.get("metadata")
    raw = metadata.get("raw") if isinstance(metadata, Mapping) else None
    candidates: list[object] = []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, Mapping):
        inner = raw.get("error")
        candidates += (
            [inner.get("code"), inner.get("type")] if isinstance(inner, Mapping) else [raw.get("code")]
        )
    candidates.append(error.get("code"))
    return next((c for c in candidates if isinstance(c, str) and _CODE.fullmatch(c)), None)


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    media_type: str
    model: str
    cost_usd: Decimal  # what OpenRouter charged Derp (on a contributed key: its fee only)
    upstream_usd: Decimal | None = None  # what a contributed key's provider charged its owner
    key_id: int | None = None  # the contributed key that paid it


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    model: str
    cost_usd: Decimal
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Speech:
    """A voice performance as MP3, priced per character (the endpoint reports no cost)."""

    data: bytes
    model: str
    cost_usd: Decimal
    characters: int
    generation_id: str | None = None
    upstream_usd: Decimal | None = None  # never: voice lines are on Derp's credits (``Billed`` alike)
    key_id: int | None = None


@dataclass(frozen=True, slots=True)
class Decided:
    """Answers to typed questions, keyed like the questions (``{"type": "noul", "noul": 0.93}``, …)."""

    answers: dict[str, Json]
    model: str
    cost_usd: Decimal
    input_tokens: int


def _client(settings: Settings, api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.openrouter_base_url.rstrip("/") + "/",
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": settings.openrouter_app_url,
            "X-Title": settings.openrouter_app_title,
        },
        timeout=httpx.Timeout(90, connect=10),
    )


async def _send(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as error:  # timeouts and connection failures: status 0
        raise OpenRouterError(0, type(error).__name__) from None
    if response.status_code >= 400:
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        error = cast(Mapping[str, Any], error) if isinstance(error, Mapping) else {}
        metadata = error.get("metadata")
        provider = metadata.get("provider_name") if isinstance(metadata, Mapping) else None
        raise OpenRouterError(
            response.status_code,
            str(error.get("message", ""))[:200] or response.reason_phrase,
            code=_error_code(error),
            provider=str(provider)[:40] if provider else None,
        )
    return response


def _as(api_key: str | None) -> dict[str, str]:
    """Headers that make one request on a contributor's sub-key (``derp.keys``) instead of Derp's own key."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


class OpenRouterAPI:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or _client(settings, settings.openrouter_api_key)
        # Price estimates for responses without ``usage.cost``: never record a paid call as free.
        self._image_price = Decimal(str(settings.image_price_usd))
        self._stt_minute_price = Decimal(str(settings.stt_price_usd_per_minute))
        self._decision_token_price = Decimal(str(settings.decisions.price_usd_per_m_input)) / 1_000_000

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await _send(self._client, method, path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Json:
        return (await self._send(method, path, **kwargs)).json()

    # Catalog

    async def models(self, **filters: str) -> list[Json]:
        return (await self._request("GET", "models", params=filters))["data"]

    async def zdr_endpoints(self) -> list[Json]:
        return (await self._request("GET", "endpoints/zdr"))["data"]

    async def model_endpoints(self, model: str) -> list[Json]:
        return (await self._request("GET", f"models/{model}/endpoints"))["data"]["endpoints"]

    async def image_models(self) -> list[Json]:
        return (await self._request("GET", "images/models"))["data"]

    async def key_info(self) -> Json:
        return (await self._request("GET", "key"))["data"]

    async def generation(self, generation_id: str) -> Json:
        """Usage of a finished generation — the cost source for cancelled streams."""
        return (await self._request("GET", "generation", params={"id": generation_id}))["data"]

    async def ping(self, model: str, provider: str, *, api_key: str) -> None:
        """One tiny completion on ``provider``'s own endpoints, no fallbacks: does this sub-key work there?

        Raises ``OpenRouterError`` with the status the key got (401/403 rejected, 402 no credits, …).
        """
        body: Json = {
            "model": model,
            "messages": [{"role": "user", "content": "Say ok."}],
            "max_tokens": 16,
            "provider": {"only": [provider], "allow_fallbacks": False},
        }
        await self._request("POST", "chat/completions", json=body, headers=_as(api_key))

    # Images (all-or-nothing billing; no zdr flag, so ZDR hosts are pinned via provider.only)

    async def generate_image(
        self,
        model: str,
        prompt: str,
        *,
        only: Sequence[str] = (),
        zdr: bool = True,
        references: Sequence[tuple[bytes, str]] = (),
        aspect_ratio: str | None = None,
        complexity: str | None = None,
        gif: tuple[int, int] | None = None,
        api_key: str | None = None,
    ) -> GeneratedImage:
        """``api_key``: a contributor's sub-key, so their provider key pays for the image.

        On requests pinned to OpenAI (contributed keys: the owner's account and usage policy) moderation is
        "low", OpenAI's less strict setting, and ``complexity`` is how much planning GPT Image 2.6+ puts into
        the scene ("low" suits quick edits); both are OpenAI passthrough options other hosts never see.
        ``gif`` (frames, milliseconds each) asks a model that animates (GPT Image 2.6+) for a looping GIF.
        """
        if zdr and not only:
            raise ValueError(f"no ZDR host pinned for {model}; refusing an unrestricted image request")
        body: Json = {"model": model, "prompt": prompt}
        if only:
            body["provider"] = {"only": list(only), "allow_fallbacks": False}
            if "openai" in only:
                options: Json = {"moderation": "low"} | ({"complexity": complexity} if complexity else {})
                body["provider"]["options"] = {"openai": options}
        if references:
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_url(data, mime)}}
                for data, mime in references
            ]
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        if gif:  # opaque: Telegram plays animations as MP4, which has no transparency
            body |= {"output_format": "gif", "background": "opaque"}
            body["frame_count"], body["frame_duration_ms"] = gif
        result = await self._request("POST", "images", json=body, headers=_as(api_key))
        images = result.get("data") or []
        if not images:
            raise OpenRouterError(502, "no image returned")
        first: Mapping[str, Any] = images[0]
        return GeneratedImage(
            data=base64.b64decode(first["b64_json"]),
            media_type=str(first.get("media_type") or "image/png"),
            model=model,
            cost_usd=_reported_cost(result, self._image_price),
            upstream_usd=upstream_cost(result.get("usage")),
        )

    # Speech-to-text

    async def transcribe(
        self,
        model: str,
        audio: bytes,
        audio_format: str,
        *,
        zdr: bool = True,
        language: str | None = None,
        seconds: int = 0,
    ) -> Transcript:
        """``seconds`` (the recording's length) prices the call when OpenRouter reports no cost."""
        body: Json = {
            "model": model,
            "input_audio": {"data": base64.b64encode(audio).decode(), "format": audio_format},
        }
        if zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        if language:
            body["language"] = language
        result = await self._request("POST", "audio/transcriptions", json=body)
        return Transcript(
            text=str(result.get("text") or "").strip(),
            model=model,
            cost_usd=_reported_cost(result, self._stt_minute_price * max(seconds, 1) / 60),
            language=result.get("language"),
        )

    # Text-to-speech (raw MP3 back, no usage: priced per character by the caller)

    async def speak(
        self, model: str, text: str, voice: str, *, cost_usd: Decimal, zdr: bool = True
    ) -> Speech:
        """``cost_usd``: what the line costs by its characters, which the response never reports."""
        body: Json = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        if zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        response = await self._send("POST", "audio/speech", json=body)
        if not response.content:
            raise OpenRouterError(502, "no audio returned")
        return Speech(
            data=response.content,
            model=model,
            cost_usd=cost_usd,
            characters=len(text),
            generation_id=response.headers.get("x-generation-id"),
        )

    # Decisions (System One API: Jev; output tokens are free)

    async def decide(self, model: str, state: str | Json, questions: Json, *, zdr: bool = True) -> Decided:
        """Ask typed questions (``noul``/``choice``/``score``) about ``state``; every question gets its answer.

        The ``v1/systemone`` endpoint takes the same body and provider preferences as the alpha Decisions API.
        """
        body = decision_body(model, state, questions, zdr=zdr)
        result = await self._request("POST", "systemone", json=body)
        answers = result.get("answers")
        if not isinstance(answers, dict) or answers.keys() != questions.keys():
            raise OpenRouterError(502, "answers do not match the questions")
        tokens = int((result.get("usage") or {}).get("input_tokens") or estimated_tokens(body))
        return Decided(
            answers=cast(dict[str, Json], answers),
            model=str(result.get("model") or model),
            cost_usd=_reported_cost(result, self._decision_token_price * tokens),
            input_tokens=tokens,
        )


def decision_body(model: str, state: str | Json, questions: Json, *, zdr: bool = True) -> Json:
    body: Json = {"model": model, "state": state, "questions": questions}
    if zdr:
        body["provider"] = {"zdr": True, "data_collection": "deny"}
    return body


def estimated_tokens(body: Json) -> int:
    """Input tokens of a request the provider reported none for: UTF-8 bytes / 4."""
    return len(json.dumps(body).encode()) // 4


def upstream_cost(usage: object) -> Decimal | None:
    """What the provider behind a contributed key charged for a call (OpenRouter's ``upstream_inference_cost``),
    None when the call didn't go through one."""
    if not isinstance(usage, dict) or not cast(Json, usage).get("is_byok"):
        return None
    details = cast(Json, usage).get("cost_details") or {}
    cost = details.get("upstream_inference_cost") if isinstance(details, dict) else None
    return Decimal(str(cost)) if cost is not None else None


def _reported_cost(result: Json, estimate: Decimal) -> Decimal:
    """OpenRouter's ``usage.cost`` when present (0 included), else the configured estimate."""
    cost = (result.get("usage") or {}).get("cost")
    return Decimal(str(cost)) if cost is not None else estimate.quantize(Decimal("0.00000001"))


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


@dataclass(frozen=True, slots=True)
class SubKey:
    """An OpenRouter API key made for one contributor: ``secret`` is shown once, ``hash`` names it afterwards."""

    secret: str
    hash: str


@dataclass(frozen=True, slots=True)
class Credential:
    """A provider key registered on Derp's OpenRouter account (BYOK); ``label`` is OpenRouter's masked form."""

    id: str
    label: str


class OpenRouterManagement:
    """The management API (its own key, which can't make calls): contributors' sub-keys and their provider keys.

    Each contributed key is registered locked to its own sub-key (``allowed_api_key_hashes``) and to the
    models it may serve, with OpenRouter's shared capacity removed for those models (``is_required``): a
    request through the sub-key runs on the contributor's key or fails, and no other key can spend it.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or _client(settings, settings.openrouter_management_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Json:
        response = await _send(self._client, method, path, **kwargs)
        return response.json() if response.content else {}

    async def create_key(self, name: str, *, limit_usd: float) -> SubKey:
        """A sub-key whose own spending (Derp's credits: web search, BYOK fees) stops at ``limit_usd`` a month."""
        body = {"name": name, "limit": limit_usd, "limit_reset": "monthly", "include_byok_in_limit": False}
        result = await self._request("POST", "keys", json=body)
        return SubKey(secret=str(result["key"]), hash=str(result["data"]["hash"]))

    async def delete_key(self, key_hash: str) -> None:
        await self._request("DELETE", f"keys/{key_hash}")

    async def register(
        self, provider: str, key: str, *, name: str, sub_key_hash: str, models: Sequence[str]
    ) -> Credential:
        body: Json = {
            "provider": provider,
            "key": key,
            "name": name,
            "allowed_api_key_hashes": [sub_key_hash],
            "allowed_models": list(models),
            "is_required": True,
            "declared_zdr": False,  # a subscription's API org has no ZDR agreement
        }
        data = (await self._request("POST", "byok", json=body))["data"]
        return Credential(id=str(data["id"]), label=str(data.get("label") or ""))

    async def allow_models(self, credential_id: str, models: Sequence[str]) -> None:
        await self._request("PATCH", f"byok/{credential_id}", json={"allowed_models": list(models)})

    async def unregister(self, credential_id: str) -> None:
        await self._request("DELETE", f"byok/{credential_id}")
