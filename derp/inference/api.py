"""Small OpenRouter REST client for what Pydantic AI does not cover:
image generation, speech-to-text, the model catalog and generation lookups.
"""

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

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

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Json:
        response = await self._client.request(method, path, **kwargs)
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
        references: Sequence[tuple[bytes, str]] = (),
        aspect_ratio: str | None = None,
    ) -> GeneratedImage:
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
            cost_usd=Decimal(str((result.get("usage") or {}).get("cost") or 0)),
        )

    # Speech-to-text

    async def transcribe(
        self, model: str, audio: bytes, audio_format: str, *, zdr: bool = True, language: str | None = None
    ) -> Transcript:
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
            cost_usd=Decimal(str((result.get("usage") or {}).get("cost") or 0)),
            language=result.get("language"),
        )


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
