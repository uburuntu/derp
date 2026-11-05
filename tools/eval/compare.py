"""Compare two eval runs scenario by scenario: uv run python -m tools.eval.compare BEFORE AFTER

Per scenario: passes, average model requests, average input tokens a request and the tools of the
latest run's first try; then totals over all scenarios and over the ones both runs have. Repeated runs
(``--repeat``) are averaged; one run per side is mostly noise, three is a minimum for a decision.
"""

import json
import sys
from collections import defaultdict
from statistics import mean
from typing import Any

from tools.eval.run import OUT

type Rows = dict[str, list[dict[str, Any]]]


def load(label: str) -> Rows:
    rows: Rows = defaultdict(list)
    for line in (OUT / f"{label}.jsonl").read_text().splitlines():
        row = json.loads(line)
        rows[row["name"]].append(row)
    return rows


def totals(rows: Rows, names: set[str] | None = None) -> dict[str, object]:
    runs = [r for name, rs in rows.items() if names is None or name in names for r in rs]
    ran = [r for r in runs if r["requests"]]
    requests = sum(r["requests"] for r in ran)
    return {
        "passed": f"{sum(r['passed'] for r in runs)}/{len(runs)}",
        "cost": round(sum(r["cost"] + r["side_cost"] for r in runs), 4),
        "chat_cost": round(sum(r["cost"] for r in runs), 4),
        "requests": requests,
        "input": sum(r["input_tokens"] for r in ran),
        "per_request": round(sum(r["input_tokens"] for r in ran) / max(requests, 1)),
        "cache": sum(r["cache_tokens"] for r in ran),
        "output": sum(r["output_tokens"] for r in ran),
    }


def main(before: str, after: str) -> None:
    a, b = load(before), load(after)
    print(f"{'scenario':20} {'passes':13} {'requests':10} {'in/request':14} tools (after)")
    for name in sorted(set(a) | set(b)):
        ra, rb = a.get(name, []), b.get(name, [])
        marks = ["".join("✅" if r["passed"] else "❌" for r in rs) or "–" for rs in (ra, rb)]
        requests = [mean(r["requests"] for r in rs) if rs else 0 for rs in (ra, rb)]
        per = [mean(r["input_tokens"] / max(r["requests"], 1) for r in rs) if rs else 0 for rs in (ra, rb)]
        tools = [t for t, _ in rb[-1]["tools"]] if rb else []
        failed = rb[-1]["error"] if rb and not rb[-1]["passed"] else ""
        print(
            f"{name:20} {marks[0]}→{marks[1]:6} {requests[0]:3.1f}→{requests[1]:<5.1f} "
            f"{per[0]:6.0f}→{per[1]:<7.0f} {tools} {failed}"
        )
    common = set(a) & set(b)
    print(f"\nall     {before}: {totals(a)}\nall     {after}: {totals(b)}")
    print(f"common  {before}: {totals(a, common)}\ncommon  {after}: {totals(b, common)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
