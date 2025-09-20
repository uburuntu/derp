from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

import logfire
from aiohttp import web
from aiogram import Bot
from aiogram.utils.web_app import check_webapp_signature
from google import genai
from google.genai import types

from ..common.llm_gemini import Gemini
from ..config import settings


def _static_path() -> Path:
    return Path(__file__).with_name("static")


async def _demo(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_static_path() / "demo.html")


async def _check_auth(request: web.Request) -> tuple[bool, dict]:
    bot: Bot = request.app["bot"]
    if request.content_type.startswith("multipart/"):
        data = dict(await request.post())
    else:
        data = await request.post()
    token = data.get("_auth", "")
    if not token:
        return False, data
    try:
        ok = check_webapp_signature(bot.token, token)
        return ok, data
    except Exception:
        return False, data


async def _generate_text(request: web.Request) -> web.Response:
    ok, data = await _check_auth(request)
    if not ok:
        return web.json_response({"ok": False, "err": "Unauthorized"}, status=401)

    prompt = (data.get("prompt") or "").strip()
    image_bytes: Optional[bytes] = None
    image_mime: Optional[str] = None

    image_field = data.get("image")
    if hasattr(image_field, "file"):
        image_bytes = image_field.file.read()
        image_mime = getattr(image_field, "content_type", None) or "image/png"

    try:
        gem = Gemini()
        req = gem.create_request().with_text(prompt or "")
        if image_bytes:
            req.with_media(image_bytes, image_mime or "image/png")
        result = await req.execute()
        return web.json_response({"ok": True, "text": result.full_text})
    except Exception as e:
        logfire.exception("Gemini text generation failed")
        return web.json_response({"ok": False, "err": str(e)}, status=500)


def _get_image_client() -> genai.Client:
    api_key = settings.google_api_paid_key
    if not api_key:
        raise RuntimeError("Google API key is required for image generation")
    return genai.Client(api_key=api_key)


def _extract_inline_images(response) -> list[tuple[bytes, str]]:
    images: list[tuple[bytes, str]] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return images
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            mime = getattr(inline, "mime_type", "") if inline else ""
            if inline and mime.startswith("image/"):
                data = getattr(inline, "data", b"")
                if isinstance(data, str):
                    try:
                        data = base64.b64decode(data)
                    except Exception:
                        continue
                if isinstance(data, (bytes, bytearray)):
                    images.append((bytes(data), mime))
    except Exception:
        logfire.exception("Failed to extract images from Gemini response")
    return images


async def _generate_image(request: web.Request) -> web.Response:
    ok, data = await _check_auth(request)
    if not ok:
        return web.json_response({"ok": False, "err": "Unauthorized"}, status=401)

    prompt = (data.get("prompt") or "").strip()

    try:
        client = _get_image_client()
        contents = [types.Part.from_text(text=prompt or "A cute robot doodle")]
        response = client.models.generate_content(
            model="gemini-2.5-flash-image-preview",
            contents=contents,
        )
        images = _extract_inline_images(response)
        if not images:
            return web.json_response({"ok": False, "err": "No image generated"}, status=500)
        data_bytes, mime = images[0]
        b64 = base64.b64encode(data_bytes).decode("ascii")
        return web.json_response({"ok": True, "image_b64": b64, "mime": mime})
    except Exception as e:
        logfire.exception("Gemini image generation failed")
        return web.json_response({"ok": False, "err": str(e)}, status=500)


def create_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/webapp", _demo)
    app.router.add_get("/webapp/demo", _demo)  # alias
    app.router.add_post("/webapp/generateText", _generate_text)
    app.router.add_post("/webapp/generateImage", _generate_image)
    return app

