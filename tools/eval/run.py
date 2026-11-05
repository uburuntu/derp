"""Run the eval scenarios against the live models. Spends real money (a full run is about $0.10).

    make db-test-up
    uv run python -m tools.eval.run LABEL [--only a,b] [--tags x,y] [--repeat N] [--jobs N] [--record]

Each scenario gets a fresh synthetic group (``world.seed``) and one ask, then waits for the run and
checks what Derp did (``scenarios``). Results go to ``tools/eval/out/LABEL.jsonl`` (gitignored);
``--record`` also keeps the chat requests' bodies (``LABEL.bodies.json``) for ``tools.eval.tokens``.
Compare two runs with ``tools.eval.compare``. To A/B against an older commit, run that commit's own
copy from a ``git worktree`` (its own ``uv sync``) with ``EVAL_DATABASE_URL`` pointing at another
database, so both can run at once.
"""

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx2

from derp.agent.models import AgentRun
from derp.inference.openrouter import build_provider
from tests.e2e.harness import Room
from tools.eval.scenarios import SCENARIOS, Result, Scenario
from tools.eval.world import World, seed

OUT = Path(__file__).parent / "out"
SENT_METHODS = ("send", "setMessageReaction", "pinChatMessage", "edit", "delete")


class Recording(httpx2.AsyncBaseTransport):
    """Keeps every chat completion request body that goes to OpenRouter."""

    def __init__(self, bodies: list[Any]) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.bodies = bodies

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/chat/completions"):
            self.bodies.append(json.loads(request.content))
        return await self.inner.handle_async_request(request)


def _content(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(_content(item) for item in value)
    if isinstance(value, dict):
        return f"[{value.get('kind') or value.get('media_type') or 'object'}]"
    return str(value)


def read_run(result: Result, run: AgentRun) -> None:
    """The run's tool calls, results, retries and final text, from its stored messages."""
    result.status, result.model, result.error = run.status, run.served_model or "", run.error_type
    result.input_tokens, result.cache_tokens = run.input_tokens, run.cache_read_tokens
    result.output_tokens, result.cost = run.output_tokens, float(run.cost_usd)
    for message in run.messages or []:
        if message.get("kind") == "response":
            result.requests += 1
        texts: list[str] = []
        for part in message.get("parts", []):
            match part.get("part_kind"):
                case "tool-call":
                    args = part.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"<raw>": args}
                    result.tools.append((part.get("tool_name", ""), args))
                case "tool-return":
                    result.returns.append((part.get("tool_name", ""), _content(part.get("content"))[:600]))
                case "retry-prompt":
                    result.retries.append((part.get("tool_name") or "", _content(part.get("content"))[:400]))
                case "text" if message.get("kind") == "response":
                    texts.append(part.get("content") or "")
        if texts:
            result.text = "".join(texts).strip()


async def one(world: World, scenario: Scenario, index: int) -> Result:
    result = Result(scenario.name)
    room = Room(-1009000000 - index, title="Тусовка")
    started = time.monotonic()
    try:
        seeded = await seed(world, room)
        if scenario.setup is not None:
            await scenario.setup(world, seeded)
        reply_to = seeded.ids[scenario.reply_to] if scenario.reply_to else None
        sent = await world.ask(scenario.asker, room, scenario.text, reply_to=reply_to)
        run = await world.run_of(sent)
        result.seconds = round(time.monotonic() - started, 1)
        if run is None:
            result.status, result.error = "no run", "FAIL: no run"
            return result
        read_run(result, run)
        result.side_cost = float(
            sum(c.cost_usd for c in await world.provider_calls(run.id) if c.kind != "chat")
        )
        result.outputs = [
            call.method
            for call in world.api.calls[sent.after :]
            if str(call.fields.get("chat_id", "")) == str(room.id)
            and call.method.startswith(SENT_METHODS)
            and call.method not in ("sendChatAction", "sendRichMessageDraft")
        ]
        result.passed, why = scenario.check(result, seeded)
        result.error = result.error or ("" if result.passed else f"FAIL: {why}")
    except Exception as error:  # noqa: BLE001 - one broken scenario must not stop the others
        result.error = f"{type(error).__name__}: {error}"
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("label")
    parser.add_argument("--only", default="", help="comma-separated scenario names")
    parser.add_argument("--tags", default="", help="comma-separated tags")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=8, help="scenarios at once")
    parser.add_argument("--record", action="store_true", help="keep the chat request bodies")
    args = parser.parse_args()
    only, tags = set(filter(None, args.only.split(","))), set(filter(None, args.tags.split(",")))
    chosen = [s for s in SCENARIOS if (not only or s.name in only) and (not tags or tags & set(s.tags))]
    OUT.mkdir(exist_ok=True)
    world = await World.start()
    bodies: list[Any] = []
    if args.record and world.conv.services is not None:
        client = httpx2.AsyncClient(transport=Recording(bodies), timeout=120)
        world.conv.services.provider = build_provider(world.conv.settings, client)
    gate = asyncio.Semaphore(args.jobs)
    results: list[Result] = []

    async def guarded(scenario: Scenario, index: int) -> None:
        async with gate:
            r = await one(world, scenario, index)
        results.append(r)
        print(
            f"{'✅' if r.passed else '❌'} {r.name:20} {r.seconds:5}s req={r.requests} in={r.input_tokens} "
            f"cache={r.cache_tokens} out={r.output_tokens} ${r.cost + r.side_cost:.4f} "
            f"tools={[t for t, _ in r.tools]} {r.error or ''}",
            flush=True,
        )

    try:
        await asyncio.gather(
            *(guarded(s, n * 100 + i) for n in range(args.repeat) for i, s in enumerate(chosen))
        )
    finally:
        await world.stop()
    with (OUT / f"{args.label}.jsonl").open("w") as out:
        for r in results:
            out.write(json.dumps(asdict(r), ensure_ascii=False, default=str) + "\n")
    if args.record:
        (OUT / f"{args.label}.bodies.json").write_text(json.dumps(bodies, ensure_ascii=False, default=str))
    ran = [r for r in results if r.requests]
    print(
        f"\n{args.label}: {sum(r.passed for r in results)}/{len(results)} passed, "
        f"${sum(r.cost + r.side_cost for r in results):.3f}, input tokens {sum(r.input_tokens for r in ran)}, "
        f"{sum(r.requests for r in ran) / max(len(ran), 1):.2f} requests a run"
    )


if __name__ == "__main__":
    asyncio.run(main())
