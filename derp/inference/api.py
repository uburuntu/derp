"""Small OpenRouter REST client for what Pydantic AI does not cover:
image generation, speech-to-text, decisions, the model catalog and generation lookups.
"""

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import httpx

from derp.config import Settings

type Json = dict[str, Any]


class OpenRouterError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"OpenRouter {status}: {message}")

    @property
    def retryable(self) -> bool:
        return self.status in {408, 429, 500, 502, 503, 504, 529}


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    media_type: str
    model: str
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    model: str
    cost_usd: Decimal
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Decided:
    """Answers to typed questions, keyed like the questions (``{"type": "noul", "noul": 0.93}``, …)."""

    answers: dict[str, Json]
    model: str
    cost_usd: Decimal
    input_tokens: int


class OpenRouterAPI:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=settings.openrouter_base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": settings.openrouter_app_url,
                "X-Title": settings.openrouter_app_title,
            },
            timeout=httpx.Timeout(90, connect=10),
        )
        # Price estimates for responses without ``usage.cost``: never record a paid call as free.
        self._image_price = Decimal(str(settings.image_price_usd))
        self._stt_minute_price = Decimal(str(settings.stt_price_usd_per_minute))
        self._decision_token_price = Decimal(str(settings.decisions.price_usd_per_m_input)) / 1_000_000

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Json:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:  # timeouts and connection failures: status 0
            raise OpenRouterError(0, type(error).__name__) from None
        if response.status_code >= 400:
            try:
                message = str(response.json().get("error", {}).get("message", ""))[:200]
            except ValueError:
                message = ""
            raise OpenRouterError(response.status_code, message or response.reason_phrase)
        return response.json()

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
    ) -> GeneratedImage:
        if zdr and not only:
            raise ValueError(f"no ZDR host pinned for {model}; refusing an unrestricted image request")
        body: Json = {"model": model, "prompt": prompt}
        if only:
            body["provider"] = {"only": list(only), "allow_fallbacks": False}
        if references:
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_url(data, mime)}}
                for data, mime in references
            ]
        if aspect_ratio:
            body["aspect_ratio"] = aspect_ratio
        result = await self._request("POST", "images", json=body)
        images = result.get("data") or []
        if not images:
            raise OpenRouterError(502, "no image returned")
        first: Mapping[str, Any] = images[0]
        return GeneratedImage(
            data=base64.b64decode(first["b64_json"]),
            media_type=str(first.get("media_type") or "image/png"),
            model=model,
            cost_usd=_reported_cost(result, self._image_price),
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

    # Decisions (System One API: Jev; output tokens are free)

    async def decide(self, model: str, state: str | Json, questions: Json, *, zdr: bool = True) -> Decided:
        """Ask typed questions (``noul``/``choice``/``score``) about ``state``; every question gets its answer.

        The ``v1/systemone`` endpoint takes the same body and provider preferences as the alpha Decisions API.
        """
        body: Json = {"model": model, "state": state, "questions": questions}
        if zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        result = await self._request("POST", "systemone", json=body)
        answers = result.get("answers")
        if not isinstance(answers, dict) or answers.keys() != questions.keys():
            raise OpenRouterError(502, "answers do not match the questions")
        tokens = int((result.get("usage") or {}).get("input_tokens") or len(json.dumps(body).encode()) // 4)
        return Decided(
            answers=cast(dict[str, Json], answers),
            model=str(result.get("model") or model),
            cost_usd=_reported_cost(result, self._decision_token_price * tokens),
            input_tokens=tokens,
        )


def _reported_cost(result: Json, estimate: Decimal) -> Decimal:
    """OpenRouter's ``usage.cost`` when present (0 included), else the configured estimate."""
    cost = (result.get("usage") or {}).get("cost")
    return Decimal(str(cost)) if cost is not None else estimate.quantize(Decimal("0.00000001"))


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
