"""Summarise eval results.

uv run python evals/report.py                         # latest.json
uv run python evals/report.py evals/results/x.json
uv run python evals/report.py --compare baseline v1   # tags or file paths
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for k passes out of n. Unlike p ± 1.96·sqrt(p(1-p)/n)
    it stays inside [0, 1] and is sensible at small n: 3/3 is 44–100%.
    """
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pass_with_ci(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({lo:.0%}–{hi:.0%})"


def load(ref: str) -> dict:
    p = Path(ref)
    if p.exists():
        return json.loads(p.read_text())
    matches = sorted(RESULTS_DIR.glob(f"*-{ref}.json"))
    if not matches:
        raise SystemExit(f"no results file for {ref!r}")
    return json.loads(matches[-1].read_text())


def summarise(payload: dict) -> dict[str, dict]:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in payload["results"]:
        by_task[r["task"]].append(r)
    out = {}
    for task, rs in by_task.items():
        real = [r for r in rs if not r["infra_error"]] or rs
        n = len(real)
        costs = [r["cost_usd"] for r in real if r["cost_usd"] is not None]
        out[task] = {
            "passed": sum(r["passed"] for r in real),
            "runs": n,
            "trials": len(rs),
            "infra_errors": sum(r["infra_error"] for r in rs),
            "pass@1": sum(r["passed"] for r in real) / n,
            "pass@k": float(any(r["passed"] for r in real)),
            "steps": sum(r["steps"] for r in real) / n,
            "tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in real) / n,
            "reasoning": sum(r.get("reasoning_tokens", 0) for r in real) / n,
            "rejections": sum(sum(not v["approve"] for v in r.get("reviews", [])) for r in real)
            / n,
            "cached%": 100
            * sum(r["cached_tokens"] for r in real)
            / max(1, sum(r["prompt_tokens"] for r in real)),
            "cost": sum(costs) / len(costs) if costs else None,
            "seconds": sum(r["seconds"] for r in real) / n,
        }
    return out


def totals(s: dict[str, dict]) -> dict:
    n = len(s) or 1
    costs = [v["cost"] for v in s.values() if v["cost"] is not None]
    return {
        "passed": sum(v["passed"] for v in s.values()),
        "runs": sum(v["runs"] for v in s.values()),
        "trials": sum(v["trials"] for v in s.values()),
        "infra_errors": sum(v["infra_errors"] for v in s.values()),
        "pass@1": sum(v["pass@1"] for v in s.values()) / n,
        "pass@k": sum(v["pass@k"] for v in s.values()) / n,
        "steps": sum(v["steps"] for v in s.values()) / n,
        "tokens": sum(v["tokens"] for v in s.values()) / n,
        "reasoning": sum(v["reasoning"] for v in s.values()) / n,
        "rejections": sum(v["rejections"] for v in s.values()) / n,
        "cached%": sum(v["cached%"] for v in s.values()) / n,
        "cost": sum(costs) / len(costs) if costs else None,
        "seconds": sum(v["seconds"] for v in s.values()) / n,
    }


def fmt_row(name: str, v: dict) -> str:
    cost = f"${v['cost']:.4f}" if v["cost"] is not None else "   n/a "
    return (
        f"{name:<22} {v['pass@1']:>6.0%} {v['pass@k']:>6.0%} {v['steps']:>6.1f} "
        f"{v['tokens']:>8.0f} {v['cached%']:>6.1f}% {cost:>8} {v['seconds']:>6.0f}s"
        + (f"  ({v['infra_errors']} infra err)" if v["infra_errors"] else "")
    )


HEADER = (
    f"{'task':<22} {'pass@1':>6} {'pass@k':>6} {'steps':>6} "
    f"{'tokens':>8} {'cached':>7} {'cost':>8} {'time':>7}"
)


def print_report(payload: dict) -> None:
    s = summarise(payload)
    print(
        f"tag={payload['tag']} prompt={payload['prompt_version']} "
        f"models={payload['models']} ({payload['started']})"
    )
    print(HEADER)
    for task, v in s.items():
        print(fmt_row(task, v))
    print("-" * len(HEADER))
    t = totals(s)
    print(fmt_row("ALL", t))
    print("\npass rate with 95% confidence interval (the true rate is plausibly in this range):")
    for task, v in s.items():
        print(f"  {task:<22} {pass_with_ci(v['passed'], v['runs'])}")
    print(f"  {'ALL':<22} {pass_with_ci(t['passed'], t['runs'])}")


def print_compare(a: dict, b: dict) -> None:
    sa, sb = summarise(a), summarise(b)
    common = sorted(set(sa) & set(sb))
    # totals over shared tasks only: different task sets would compare different things
    ta = totals({t: sa[t] for t in common})
    tb = totals({t: sb[t] for t in common})
    print(f"{'':<22} {a['tag']:>16} {b['tag']:>16} {'delta':>10}")
    for task in sorted(set(sa) | set(sb)):
        va, vb = sa.get(task), sb.get(task)
        pa = f"{va['pass@1']:.0%}" if va else "-"
        pb = f"{vb['pass@1']:.0%}" if vb else "-"
        d = f"{(vb['pass@1'] - va['pass@1']):+.0%}" if va and vb else ""
        print(f"{task:<22} {pa:>16} {pb:>16} {d:>10}")
    print("-" * 68)
    only = sorted(set(sa) ^ set(sb))
    if only:
        print(f"ALL rows cover the {len(common)} shared task(s); excluded: {', '.join(only)}")
    for key, fmt in [
        ("pass@1", "{:.0%}"),
        ("steps", "{:.1f}"),
        ("tokens", "{:.0f}"),
        ("reasoning", "{:.0f}"),
        ("rejections", "{:.2f}"),
        ("cost", "${:.4f}"),
        ("seconds", "{:.0f}s"),
    ]:
        xa, xb = ta[key], tb[key]
        fa = fmt.format(xa) if xa is not None else "n/a"
        fb = fmt.format(xb) if xb is not None else "n/a"
        d = ""
        if xa not in (None, 0) and xb is not None:
            d = f"{(xb - xa) / xa:+.0%}"
        print(f"{'ALL ' + key:<22} {fa:>16} {fb:>16} {d:>10}")
        if key == "pass@1":
            ca, cb = pass_with_ci(ta["passed"], ta["runs"]), pass_with_ci(tb["passed"], tb["runs"])
            print(f"{'  95% interval':<22} {ca:>16} {cb:>16}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals/report.py")
    ap.add_argument("ref", nargs="?", default=str(RESULTS_DIR / "latest.json"))
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args(argv)
    if args.compare:
        print_compare(load(args.compare[0]), load(args.compare[1]))
    else:
        print_report(load(args.ref))
    return 0


if __name__ == "__main__":
    sys.exit(main())
