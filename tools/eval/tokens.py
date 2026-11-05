"""Real prompt tokens of a recorded request's parts, as the provider counts them (not estimates).

    uv run python -m tools.eval.tokens LABEL [LABEL ...] [--per-tool]

Replays the first chat request ``tools.eval.run --record`` kept for each label, with ``max_tokens=1``
and reasoning off, on one provider pinned for every label (OpenRouter's routing would otherwise mix
tokenizers): the bare persona, the web server tools, all tools, and the whole request. ``--per-tool``
adds each function tool alone on top of the web tools, for the first label. OpenRouter re-serializes
function schemas while its server tools are on, so only these counts say what a schema change saves.
Costs a fraction of a cent per call.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from derp.config import Settings
from tools.eval.run import OUT
from tools.eval.world import REPO

URL = "https://openrouter.ai/api/v1/chat/completions"
HI = [{"role": "user", "content": "hi"}]
PIN: dict[str, Any] = {}


def prompt_tokens(key: str, body: dict[str, Any]) -> int:
    request = {
        k: v
        for k, v in body.items()
        if k not in ("stream", "stream_options", "session_id", "models", "usage")
    }
    request |= {
        "max_tokens": 1,
        "reasoning": {"enabled": False},
        "provider": dict(body.get("provider") or {}, **PIN),
    }
    reply: dict[str, Any] = {}
    for _ in range(3):
        reply = httpx.post(URL, json=request, headers={"Authorization": f"Bearer {key}"}, timeout=120).json()
        if "usage" in reply:
            return int(reply["usage"]["prompt_tokens"])
    raise SystemExit(f"no usage in OpenRouter's reply: {reply.get('error')}")


def parts(key: str, body: dict[str, Any]) -> dict[str, int]:
    web = [t for t in body["tools"] if t.get("type") != "function"]
    bare = {k: v for k, v in body.items() if k != "tools"}
    found = {
        "persona": prompt_tokens(key, bare | {"messages": [body["messages"][0], *HI]}),
        "web tools": prompt_tokens(key, body | {"messages": HI, "tools": web}),
        "all tools": prompt_tokens(key, body | {"messages": HI}),
        "first request": prompt_tokens(key, body),
    }
    return found | {"function tools": found["all tools"] - found["web tools"]}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("labels", nargs="+")
    parser.add_argument("--per-tool", action="store_true")
    args = parser.parse_args()
    key = Settings(_env_file=REPO / ".env").openrouter_api_key  # pyright: ignore[reportCallIssue]
    firsts = {label: json.loads((OUT / f"{label}.bodies.json").read_text())[0] for label in args.labels}
    model = next(iter(firsts.values()))["model"]
    probe = httpx.post(URL, json={"model": model, "messages": HI, "max_tokens": 1},
                       headers={"Authorization": f"Bearer {key}"}, timeout=120).json()  # fmt: skip
    PIN.update(only=[probe["provider"]], allow_fallbacks=False)
    print(f"{model} on {probe['provider']}")
    for label, body in firsts.items():
        print(label, parts(key, body))
    if args.per_tool:
        body = next(iter(firsts.values()))
        web = [t for t in body["tools"] if t.get("type") != "function"]
        tools = [t for t in body["tools"] if t.get("type") == "function"]
        base = prompt_tokens(key, body | {"messages": HI, "tools": web})
        with ThreadPoolExecutor(8) as pool:
            counts = pool.map(
                lambda tool: prompt_tokens(key, body | {"messages": HI, "tools": [*web, tool]}), tools
            )
            alone = sorted(
                ((n - base, t["function"]["name"]) for n, t in zip(counts, tools, strict=True)), reverse=True
            )
        for tokens, name in alone:
            print(f"  {name:22} {tokens}")
        print(f"  sum of each alone: {sum(t for t, _ in alone)}")


if __name__ == "__main__":
    main()
