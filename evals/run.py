"""Run the agent on the eval tasks.

    uv run python evals/run.py --trials 3 --tag baseline
    uv run python evals/run.py --tasks csv_to_json,word_freq --prompt v1 --tag v1
    uv run python evals/run.py --thinking off --tag no-thinking
    uv run python evals/run.py --verify 2 --tag reviewed

Results go to evals/results/<timestamp>-<tag>.json and latest.json.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.prices import cost_usd  # noqa: E402
from evals.tasks import Task, load_tasks  # noqa: E402
from patchbay.agent import context as agent_context  # noqa: E402
from patchbay.agent import verifier as agent_verifier  # noqa: E402
from patchbay.agent.loop import EV_REVIEW, EV_TOOL_CALL, EV_USAGE, Limits, run_agent  # noqa: E402
from patchbay.agent.prompts import build_initial_messages, workspace_tree  # noqa: E402
from patchbay.agent.tools import default_registry  # noqa: E402
from patchbay.config import get_settings  # noqa: E402
from patchbay.llm.client import OpenAICompatibleClient  # noqa: E402
from patchbay.llm.trace import TraceWriter, new_run_id  # noqa: E402
from patchbay.sandbox import Sandbox  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass
class RunResult:
    task: str
    trial: int
    model: str
    prompt_version: str
    status: str
    passed: bool
    steps: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float | None
    seconds: float
    check_exit: int | None
    check_output: str
    run_id: str
    summary: str | None
    error: str | None
    infra_error: bool
    thinking: str = ""
    reasoning_tokens: int = 0
    reviews: list[dict] = field(default_factory=list)


def run_one(
    task: Task,
    trial: int,
    model: str,
    prompt_version: str,
    max_steps: int | None,
    context_budget: int = 0,
    thinking: str | None = None,
    verify: int = 0,
) -> RunResult:
    run_id = new_run_id()
    trace = TraceWriter(run_id)
    settings = get_settings()
    llm = OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=model,
        temperature=settings.llm_temperature,
        thinking=settings.llm_thinking if thinking is None else thinking,
        trace=trace,
    )
    tools = default_registry()
    usage = {"prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
    counters = {"tool_calls": 0}
    reviews: list[dict] = []

    def emit(kind: str, payload: dict) -> None:
        if kind == EV_USAGE:
            usage["prompt"] += payload["prompt_tokens"]
            usage["completion"] += payload["completion_tokens"]
            usage["cached"] += payload["cached_tokens"]
            usage["reasoning"] += payload.get("reasoning_tokens", 0)
        elif kind == EV_TOOL_CALL:
            counters["tool_calls"] += 1
        elif kind == EV_REVIEW:
            reviews.append(payload)

    t0 = time.monotonic()
    sandbox = Sandbox.create(f"eval-{secrets.token_hex(3)}", repo_url=task.repo_url)
    try:
        task.seed(sandbox)
        transcript = build_initial_messages(
            task=task.prompt,
            tree=workspace_tree(sandbox),
            repo_url=task.repo_url,
            version=prompt_version,
        )
        limits = Limits(max_steps=max_steps or task.max_steps or settings.max_steps)
        outcome = run_agent(
            llm=llm,
            sandbox=sandbox,
            transcript=transcript,
            tools=tools,
            emit=emit,
            limits=limits,
            context=agent_context.from_settings(context_budget),
            verifier=agent_verifier.from_settings(llm, verify),
        )

        check_exit: int | None = None
        check_output = ""
        if task.check and outcome.status == "completed":
            for path, content in task.hidden.items():
                sandbox.write_file(path, content)
            r = sandbox.exec(task.check, timeout_s=300)
            check_exit, check_output = r.exit_code, r.output[-2000:]
    finally:
        sandbox.destroy()

    seconds = time.monotonic() - t0
    return RunResult(
        task=task.name,
        trial=trial,
        model=model,
        prompt_version=prompt_version,
        status=outcome.status,
        passed=task.passed(outcome.status, check_exit),
        steps=outcome.steps,
        tool_calls=counters["tool_calls"],
        prompt_tokens=usage["prompt"],
        completion_tokens=usage["completion"],
        cached_tokens=usage["cached"],
        cost_usd=cost_usd(model, usage["prompt"], usage["completion"], usage["cached"]),
        seconds=round(seconds, 1),
        check_exit=check_exit,
        check_output=check_output,
        run_id=run_id,
        summary=outcome.summary or outcome.question,
        error=outcome.error,
        infra_error=bool(outcome.error and "LLM call failed" in outcome.error),
        thinking=llm.thinking,
        reasoning_tokens=usage["reasoning"],
        reviews=reviews,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals/run.py")
    ap.add_argument("--tasks", help="comma-separated task names (default: all)")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--prompt", help="system prompt version, e.g. v1 (default: PROMPT_VERSION)")
    ap.add_argument(
        "--models", help="comma-separated model ids, used round-robin (default: LLM_MODEL)"
    )
    ap.add_argument("--max-steps", type=int)
    ap.add_argument(
        "--context-budget", type=int, default=0, help="compact old tool outputs past N tokens"
    )
    ap.add_argument("--thinking", help="off | low | high | max (default: LLM_THINKING)")
    ap.add_argument(
        "--verify", type=int, default=0, metavar="N", help="review each finish, reject ≤ N times"
    )
    args = ap.parse_args(argv)

    tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
    args.prompt = args.prompt or get_settings().prompt_version
    models = args.models.split(",") if args.models else [get_settings().llm_model]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{stamp}-{args.tag}.json"

    results: list[RunResult] = []
    i = 0
    print(f"{len(tasks)} task(s) × {args.trials} trial(s) · prompt {args.prompt} · models {models}")
    for trial in range(1, args.trials + 1):
        for task in tasks:
            model = models[i % len(models)]
            i += 1
            print(f"[{i:>2}] {task.name} (trial {trial}, {model}) ... ", end="", flush=True)
            r = run_one(
                task,
                trial,
                model,
                args.prompt,
                args.max_steps,
                args.context_budget,
                thinking=args.thinking,
                verify=args.verify,
            )
            results.append(r)
            mark = "PASS" if r.passed else ("ERR " if r.infra_error else "FAIL")
            cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "n/a"
            tok = r.prompt_tokens + r.completion_tokens
            line = f"{mark}  {r.status:<16} {r.steps:>2} steps  {tok:>6} tok  {cost}"
            print(f"{line}  {r.seconds:>4.0f}s")
            payload = {
                "tag": args.tag,
                "prompt_version": args.prompt,
                "context_budget": args.context_budget,
                "thinking": args.thinking,
                "verify": args.verify,
                "models": models,
                "started": stamp,
                "results": [asdict(x) for x in results],
            }
            out.write_text(json.dumps(payload, indent=1))  # save after every run
            (RESULTS_DIR / "latest.json").write_text(json.dumps(payload, indent=1))

    n_pass = sum(r.passed for r in results)
    print(f"\n{n_pass}/{len(results)} passed → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
