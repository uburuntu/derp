"""Live prompt-cache probe: how long does each provider route keep a prefix warm? Spends real money.

Runs where the key is (it never leaves the server), feeding the script on stdin:

    ssh <prod host> 'docker exec -i derp python - \\
        --route deepseek/deepseek-v4.1-flash-1017@morph/fp8 --route z-ai/glm-5.3-flash@wafer/us' \\
        < tools/context/probe.py > probe.jsonl

Each route (``model@endpoint slug`` from OpenRouter's endpoint list) gets its own synthetic ~22k-token
prefix with a random nonce, sent with a short changing question after gaps of 5 seconds and 1, 3, 6 and
10 minutes, pinned to that endpoint with ZDR and no fallbacks. Each line is one request: tokens sent,
tokens read from cache, cost, seconds to the answer. About a cent per route. GLM can't turn reasoning
off; give it ``--reasoning '{"effort": "low", "exclude": true}'``.
"""

import argparse
import asyncio
import json
import secrets
import time
from typing import Any

import httpx

from derp.config import get_settings

GAPS = (5, 60, 180, 360, 600)  # seconds between consecutive requests
LINES = 900  # transcript lines in the prefix: about 22k tokens


def transcript(nonce: str) -> str:
    lines = [f"probe {nonce}: a synthetic chat transcript for a prompt-cache test."]
    lines += [
        f"[{10 + i // 60:02d}:{i % 60:02d}] #{1000 + i} Member{i % 17}: сообщение {i} про кэш"
        for i in range(LINES)
    ]
    return "\n".join(lines)


async def ask(
    client: httpx.AsyncClient, model: str, endpoint: str, text: str, step: int, reasoning: Any
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Answer with one word."},
            {"role": "user", "content": text},
            {"role": "user", "content": f"Question {step}: say ok."},
        ],
        "max_tokens": 8,
        "reasoning": reasoning,
        "usage": {"include": True},
        "provider": {"only": [endpoint], "allow_fallbacks": False, "zdr": True, "data_collection": "deny"},
    }
    started = time.monotonic()
    response = await client.post("/chat/completions", json=body)
    elapsed = round(time.monotonic() - started, 2)
    if response.status_code != 200:
        return {"status": response.status_code, "seconds": elapsed}
    usage = response.json().get("usage") or {}
    return {
        "status": 200,
        "prompt": usage.get("prompt_tokens"),
        "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "cost": usage.get("cost"),
        "seconds": elapsed,
    }


async def probe(client: httpx.AsyncClient, route: str, reasoning: Any) -> None:
    model, _, endpoint = route.partition("@")
    text = transcript(secrets.token_hex(8))
    waited = 0
    for step, gap in enumerate((0, *GAPS)):
        await asyncio.sleep(gap)
        waited += gap
        try:
            result = await ask(client, model, endpoint, text, step, reasoning)
        except httpx.HTTPError as error:
            result = {"error": type(error).__name__}
        print(json.dumps({"route": route, "gap": gap, "at": waited, **result}), flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--route", action="append", required=True, help="model@endpoint; repeatable")
    parser.add_argument("--reasoning", default='{"enabled": false}', help="the request's reasoning object")
    args = parser.parse_args()
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    async with httpx.AsyncClient(
        base_url=settings.openrouter_base_url, headers=headers, timeout=120
    ) as client:
        await asyncio.gather(*(probe(client, route, json.loads(args.reasoning)) for route in args.route))


if __name__ == "__main__":
    asyncio.run(main())
