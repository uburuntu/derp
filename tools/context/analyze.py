"""Cache share and input cost per strategy from ``simulate.py`` output.

    uv run python tools/context/analyze.py runs.jsonl --ttl 5,10,30 --price morph=0.032/0.005

A run's first model call reads the static prefix (persona and tools), the window (chat card included)
and its situation and current message; it is served from cache up to the prefix it shares with the
topic's previous request when that request is at most ``--ttl`` minutes old (the static prefix alone
otherwise: every chat shares it, so ``--cold-prefix cold`` charges it in full instead). Each later call
of the run (a tool round) reads its previous call from cache and pays for what that round added.
``--hit`` scales what a warm cache serves, for providers that keep only part of it. A strategy with a
compaction summary also pays for each rewrite (reading what it replaces, writing the summary at
``--output`` USD per 1M tokens). Costs are input only otherwise (output doesn't depend on the window),
in USD per 1,000 runs.
"""

import argparse
import json
from collections import defaultdict
from typing import Any

# DeepSeek v4.1 Flash ZDR routes (OpenRouter's endpoint list, 2026-11), USD per 1M tokens: input/cache read.
PRICES = {
    "morph": (0.032, 0.005),
    "particle": (0.05, 0.003),
    "wafer": (0.038, 0.01),
    "deepinfra": (0.098, 0.0029),
}

type Row = dict[str, Any]


def load(path: str) -> tuple[Row, dict[str, list[Row]]]:
    meta: Row = {}
    runs: dict[str, list[Row]] = defaultdict(list)
    with open(path) as lines:
        for line in lines:
            row = json.loads(line)
            if "meta" in row:
                meta = row["meta"]
            else:
                runs[row["s"]].append(row)
    return meta, runs


def evaluate(
    rows: list[Row],
    meta: Row,
    prices: dict[str, tuple[float, float]],
    *,
    ttl: float,
    cold_prefix: bool = True,
    hit: float = 1.0,
    output: float = 0.5,
) -> Row:
    total = cached_total = first_total = first_cached = runs = cold_runs = restarts = 0
    window = replayed = 0
    cost = dict.fromkeys(prices, 0.0)
    for row in rows:
        if row["tier"] == "free" or not row["calls"]:  # free models cost nothing either way
            continue
        runs += 1
        static = meta["prefix"][row["tier"]]
        cold = row["gap"] is None or row["gap"] > ttl
        cold_runs += cold
        restarts += row["restart"]
        prompt = static + row["window"] + row["situation"]
        warm = static + int(row["shared"] * hit)
        calls = [(prompt, (static if cold_prefix else 0) if cold else warm)]
        for added in row["steps"]:
            calls.append((prompt + added, prompt))
            prompt += added
        for sent, served in calls:
            total += sent
            cached_total += served
            for name, (fresh, reread) in prices.items():
                cost[name] += ((sent - served) * fresh + served * reread) / 1e6
        compaction = row.get("compacted", 0), row.get("summary", 0)  # a summary rewrite: reads, writes
        for name, (fresh, _reread) in prices.items():
            cost[name] += (compaction[0] * fresh + compaction[1] * output) / 1e6
        first_total += calls[0][0]
        first_cached += calls[0][1]
        window += row["window"]
        replayed += row["replayed"]
    n = max(runs, 1)
    return {
        "runs": runs,
        "cold": cold_runs,
        "restarts": restarts,
        "share": cached_total / max(total, 1),
        "first": first_cached / max(first_total, 1),
        "input_k": total / n / 1000,
        "window_k": window / n / 1000,
        "replay_k": replayed / n / 1000,
        **{f"${name}": value / n * 1000 for name, value in cost.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path")
    parser.add_argument("--ttl", default="10", help="minutes a provider keeps a prefix; comma-separated")
    parser.add_argument("--price", action="append", default=[], help="name=input/cache_read (USD per 1M)")
    parser.add_argument("--cold-prefix", choices=("warm", "cold"), default="warm")
    parser.add_argument("--hit", type=float, default=1.0)
    parser.add_argument("--output", type=float, default=0.5, help="USD per 1M output tokens, for summaries")
    args = parser.parse_args()
    prices = dict(PRICES)
    if args.price:
        prices = {}
        for spec in args.price:
            name, _, pair = spec.partition("=")
            fresh, _, reread = pair.partition("/")
            prices[name] = (float(fresh), float(reread))
    meta, runs = load(args.path)
    print(json.dumps(meta))
    for ttl in (float(value) for value in args.ttl.split(",")):
        print(f"\nTTL {ttl:g} min, static prefix {args.cold_prefix} after it, hit {args.hit:g}")
        header = False
        for name, rows in runs.items():
            result = evaluate(
                rows,
                meta,
                prices,
                ttl=ttl,
                cold_prefix=args.cold_prefix == "warm",
                hit=args.hit,
                output=args.output,
            )
            if not header:
                print(f"{'strategy':20}" + "".join(f"{key:>10}" for key in result))
                header = True
            cells = "".join(f"{v:>10.3f}" if isinstance(v, float) else f"{v:>10}" for v in result.values())
            print(f"{name:20}{cells}")


if __name__ == "__main__":
    main()
