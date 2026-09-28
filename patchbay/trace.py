"""Trace viewer.

uv run python -m patchbay.trace                 # list recent runs
uv run python -m patchbay.trace <run_id>        # one line per step
uv run python -m patchbay.trace <run_id> --step 4   # the full request/response of step 4
uv run python -m patchbay.trace <run_id> --system   # the system prompt as sent
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TRACES_DIR = Path("data/traces")


def load(run_id: str) -> list[dict]:
    path = TRACES_DIR / f"{run_id}.jsonl"
    if not path.exists():
        matches = sorted(TRACES_DIR.glob(f"*{run_id}*.jsonl"))
        if len(matches) != 1:
            raise SystemExit(f"no unique trace for {run_id!r} in {TRACES_DIR}")
        path = matches[0]
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def list_runs(limit: int = 20) -> None:
    files = sorted(TRACES_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[
        :limit
    ]
    for p in files:
        lines = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        if not lines:
            continue
        last = lines[-1]
        tokens = sum(x["usage"]["prompt_tokens"] + x["usage"]["completion_tokens"] for x in lines)
        end = (
            "error"
            if last["error"]
            else (
                last["response"]["tool_calls"][0]["name"]
                if last["response"]["tool_calls"]
                else "text"
            )
        )
        print(f"{p.stem}  {last['model']:<22} {len(lines):>3} steps  {tokens:>7} tok  ends: {end}")


def summarise_call(tc: dict) -> str:
    a = tc["arguments"]
    name = tc["name"]
    if name == "bash":
        return f"$ {a.get('command', '')}"
    if name in ("read_file", "write_file", "edit_file"):
        return a.get("path", "")
    if name == "search":
        return f"/{a.get('pattern', '')}/"
    if name in ("finish", "ask_user"):
        return (a.get("summary") or a.get("question") or "")[:80].replace("\n", " ")
    return json.dumps(a)[:80]


def show_steps(lines: list[dict]) -> None:
    first = lines[0]
    print(f"run {first['run_id']}  model {first['model']}  {len(lines)} steps")
    print(f"{'step':>4} {'latency':>8} {'prompt':>7} {'compl':>6} {'cached':>6}  what")
    prev_len = 0
    for x in lines:
        u = x["usage"]
        r = x["response"]
        n_msgs = len(x["request"]["messages"])
        new_msgs = n_msgs - prev_len
        prev_len = n_msgs
        if x["error"]:
            what = f"ERROR {x['error'][:100]}"
        elif r["tool_calls"]:
            what = "; ".join(f"{tc['name']} {summarise_call(tc)}" for tc in r["tool_calls"])
        else:
            what = f"text: {(r['content'] or '')[:90]!r}"
        nums = f"{x['latency_ms']:>6}ms {u['prompt_tokens']:>7} {u['completion_tokens']:>6}"
        print(f"{x['step']:>4} {nums} {u['cached_tokens']:>6}  {what}  (+{new_msgs} msgs)")


def show_step(lines: list[dict], step: int, prev_only: bool = True) -> None:
    x = next((line for line in lines if line["step"] == step), None)
    if x is None:
        raise SystemExit(f"no step {step} (run has {len(lines)})")
    msgs = x["request"]["messages"]
    prev = next((line for line in lines if line["step"] == step - 1), None)
    start = len(prev["request"]["messages"]) if (prev and prev_only) else 0
    scope = "new since last step" if start else "all"
    print(f"=== step {step}: {len(msgs)} messages in request; showing {scope} ===")
    for m in msgs[start:]:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            calls = "; ".join(
                f"{tc['function']['name']}({tc['function']['arguments'][:200]})"
                for tc in m["tool_calls"]
            )
            print(f"\n[assistant] {m.get('content') or ''}\n  tool_calls: {calls}")
        else:
            body = m.get("content") or ""
            print(f"\n[{role}{' ' + m['tool_call_id'] if role == 'tool' else ''}]\n{body}")
    print(f"\n=== response ({x['latency_ms']} ms, {x['usage']}) ===")
    if x["error"]:
        print("ERROR:", x["error"])
    r = x["response"]
    if r["reasoning"]:
        print("[reasoning]", r["reasoning"][:2000])
    if r["content"]:
        print("[content]", r["content"])
    for tc in r["tool_calls"]:
        print(f"[tool_call] {tc['name']} {json.dumps(tc['arguments'])[:2000]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m patchbay.trace")
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--step", type=int)
    ap.add_argument(
        "--all-messages",
        action="store_true",
        help="with --step: show the whole request, not just new messages",
    )
    ap.add_argument("--system", action="store_true", help="print the system prompt as sent")
    args = ap.parse_args(argv)
    if not args.run_id:
        list_runs()
        return 0
    lines = load(args.run_id)
    if args.system:
        print(lines[0]["request"]["messages"][0]["content"])
    elif args.step:
        show_step(lines, args.step, prev_only=not args.all_messages)
    else:
        show_steps(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
