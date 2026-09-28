"""Scores the reviewer against the hidden tests.

The agent runs without a reviewer; the reviewer judges the finished work, then
the hidden tests run. The agent nearly always passes, so a model then plants a
bug in the agent's change, the agent's last command is re-run, and the
reviewer judges again. With --untested the bug must also get past the agent's
own tests, so only the diff shows it.

    uv run python evals/judge.py --trials 4 --tag judge
    uv run python evals/judge.py --trials 4 --untested --tag judge-untested
    uv run python evals/judge.py --report judge judge-untested
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.report import pass_with_ci  # noqa: E402
from evals.tasks import Task, load_tasks  # noqa: E402
from patchbay.agent.loop import Limits, run_agent  # noqa: E402
from patchbay.agent.prompts import build_initial_messages, workspace_tree  # noqa: E402
from patchbay.agent.tools import default_registry  # noqa: E402
from patchbay.agent.verifier import Evidence, Verifier, gather_evidence  # noqa: E402
from patchbay.config import get_settings  # noqa: E402
from patchbay.llm.client import OpenAICompatibleClient  # noqa: E402
from patchbay.llm.structured import StructuredOutputError, chat_json  # noqa: E402
from patchbay.llm.types import system_message, user_message  # noqa: E402
from patchbay.sandbox import Sandbox, SandboxProtocol  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

PLANT_PROMPT = """\
You help test a code reviewer. You are shown a request and the change an agent \
made for it. Plant ONE realistic bug in the agent's change: the kind a careful \
reviewer could spot by reading the diff (an off-by-one, a wrong comparison, a \
missed edge case, swapped arguments, a wrong constant). Change code, not tests. \
Do not add comments that point at the bug.

Reply in JSON:
{"path": "file path relative to /workspace",
 "old": "exact text now in that file: one or more whole lines, occurring once",
 "new": "the replacement text",
 "bug": "one sentence: what is wrong now"}"""


class Plant(BaseModel):
    path: str
    old: str
    new: str
    bug: str


def judge_task(
    task: Task, trial: int, llm: OpenAICompatibleClient, *, untested: bool = False
) -> list[dict]:
    sandbox = Sandbox.create(f"judge-{secrets.token_hex(3)}", repo_url=task.repo_url)
    try:
        task.seed(sandbox)
        transcript = build_initial_messages(
            task=task.prompt,
            tree=workspace_tree(sandbox),
            repo_url=task.repo_url,
            version=get_settings().prompt_version,
        )
        outcome = run_agent(
            llm=llm,
            sandbox=sandbox,
            transcript=transcript,
            tools=default_registry(),
            emit=lambda kind, payload: None,
            limits=Limits(max_steps=task.max_steps or 30),
        )
        if outcome.status != "completed":
            return [{"task": task.name, "trial": trial, "kind": "skipped", "why": outcome.status}]

        evidence = gather_evidence(transcript, outcome.summary or "", sandbox)
        rows = [_judge(task, trial, "agent", llm, sandbox, evidence)]

        plant, why = _plant_bug(llm, sandbox, evidence, untested=untested)
        if plant is None:
            rows.append({"task": task.name, "trial": trial, "kind": "skipped", "why": why})
            return rows
        command = evidence.last_command
        visible = sandbox.exec(command, timeout_s=120) if command else None
        planted = Evidence(
            request=evidence.request,
            summary=evidence.summary,
            diff=gather_evidence(transcript, "", sandbox).diff,
            last_command=evidence.last_command,
            last_output=visible.output[-3000:] if visible else None,
        )
        row = _judge(task, trial, "planted", llm, sandbox, planted)
        row["bug"] = plant.bug
        row["visible_fail"] = bool(visible and visible.exit_code != 0)
        rows.append(row)
        return rows
    finally:
        sandbox.destroy()


def _judge(
    task: Task,
    trial: int,
    kind: str,
    llm: OpenAICompatibleClient,
    sandbox: SandboxProtocol,
    evidence: Evidence,
) -> dict:
    """The reviewer's verdict first, then the hidden tests' — so the reviewer
    never sees the hidden test files in the diff."""
    review = Verifier(llm).review(evidence)
    for path, content in task.hidden.items():
        sandbox.write_file(path, content)
    check = sandbox.exec(task.check, timeout_s=300)
    sandbox.exec("rm -f " + " ".join(f"'{p}'" for p in task.hidden))
    return {
        "task": task.name,
        "trial": trial,
        "kind": kind,
        "hidden_pass": check.exit_code == 0,
        "approve": None if review.verdict is None else review.verdict.approve,
        "issues": review.verdict.issues if review.verdict else [],
        "error": review.error,
        "review_tokens": review.usage.total_tokens,
    }


def _plant_bug(
    llm: OpenAICompatibleClient,
    sandbox: SandboxProtocol,
    evidence: Evidence,
    *,
    untested: bool = False,
) -> tuple[Plant | None, str]:
    messages = [
        system_message(PLANT_PROMPT),
        user_message(f"## Request\n{evidence.request}\n\n## Change\n```diff\n{evidence.diff}\n```"),
    ]
    problem = ""
    for _ in range(4 if untested else 2):
        try:
            plant, _ = chat_json(llm, messages, Plant)
        except StructuredOutputError as e:
            return None, str(e)
        try:
            text = sandbox.read_file(plant.path)
        except Exception as e:  # noqa: BLE001
            problem = f"cannot read {plant.path}: {e}"
        else:
            if text.count(plant.old) != 1:
                problem = f"`old` occurs {text.count(plant.old)} times in {plant.path}, not once"
            else:
                sandbox.write_file(plant.path, text.replace(plant.old, plant.new))
                if not (untested and evidence.last_command):
                    return plant, ""
                if sandbox.exec(evidence.last_command, timeout_s=120).exit_code == 0:
                    return plant, ""
                sandbox.write_file(plant.path, text)  # caught by the agent's tests: undo
                problem = (
                    f"the agent's own check (`{evidence.last_command}`) catches that bug. "
                    "Plant one that it does not catch"
                )
        messages += [
            {"role": "assistant", "content": plant.model_dump_json()},
            user_message(f"That did not apply: {problem}. Try again."),
        ]
    return None, problem


# ---------------------------------------------------------------------- report


def report(rows: list[dict]) -> None:
    judged = [r for r in rows if r["kind"] != "skipped" and r["approve"] is not None]
    agent = [r for r in judged if r["kind"] == "agent"]
    planted = [r for r in judged if r["kind"] == "planted"]
    confirmed = [r for r in planted if not r["hidden_pass"]]  # the hidden tests fail too
    shown = [r for r in confirmed if r.get("visible_fail")]  # the agent's own check shows it
    diff_only = [r for r in confirmed if not r.get("visible_fail")]
    unconfirmed = [r for r in planted if r["hidden_pass"]]
    skipped = sum(r["kind"] == "skipped" for r in rows)
    errors = sum(r["kind"] != "skipped" and r["approve"] is None for r in rows)

    def ok(rs: list[dict], want: bool) -> str:
        return pass_with_ci(sum(bool(r["approve"]) is want for r in rs), len(rs))

    good = [r for r in agent if r["hidden_pass"]]
    bad = [r for r in agent if not r["hidden_pass"]]
    print(f"{len(judged)} verdicts ({skipped} skipped, {errors} review errors)\n")
    print("the agent's own work")
    print(f"  hidden tests pass: approved             {ok(good, True)}")
    print(f"  hidden tests fail: rejected             {ok(bad, False)}")
    print("planted bugs the hidden tests confirm: rejected")
    print(f"  the agent's check also fails            {ok(shown, False)}")
    print(f"  only the diff shows it                  {ok(diff_only, False)}")
    print(f"  all                                     {ok(confirmed, False)}")
    print(f"planted changes the hidden tests miss:   rejected {ok(unconfirmed, False)}")
    tokens = [r["review_tokens"] for r in judged]
    if tokens:
        print(f"\ntokens per review                         {sum(tokens) / len(tokens):.0f}")

    for title, rs in [
        ("wrong rejections of the agent's work:", [r for r in good if not r["approve"]]),
        ("confirmed bugs approved:", [r for r in confirmed if r["approve"]]),
        ("changes the hidden tests miss (read these):", unconfirmed),
    ]:
        if rs:
            print(f"\n{title}")
            for r in rs:
                verdict = "approve" if r["approve"] else "reject "
                what = r.get("bug") or "; ".join(r["issues"])
                print(f"  {verdict} {r['task']} #{r['trial']}: {what[:150]}")


def _line(r: dict) -> str:
    if r["kind"] == "skipped":
        return f"skipped ({r['why'][:60]})"
    verdict = {True: "approve", False: "reject", None: "error"}[r["approve"]]
    return f"{r['kind']}: hidden {'pass' if r['hidden_pass'] else 'FAIL'}, reviewer {verdict}"


def load_rows(refs: list[str]) -> list[dict]:
    rows = []
    for ref in refs:
        p = Path(ref)
        files = [p] if p.exists() else sorted(RESULTS_DIR.glob(f"*-{ref}.json"))
        if not files:
            raise SystemExit(f"no results for {ref!r}")
        for f in files:
            rows += json.loads(f.read_text())["rows"]
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals/judge.py")
    ap.add_argument("--tasks", help="comma-separated task names (default: all that finish)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--tag", default="judge")
    ap.add_argument("--untested", action="store_true", help="plant bugs the agent's tests miss")
    ap.add_argument("--report", nargs="+", metavar="TAG", help="summarise earlier results")
    args = ap.parse_args(argv)

    if args.report:
        report(load_rows(args.report))
        return 0

    tasks = [t for t in load_tasks(args.tasks.split(",") if args.tasks else None) if t.check]
    settings = get_settings()
    llm = OpenAICompatibleClient.from_settings()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{stamp}-{args.tag}.json"
    rows: list[dict] = []
    for trial in range(1, args.trials + 1):
        for task in tasks:
            print(f"{task.name} #{trial} ... ", end="", flush=True)
            new = judge_task(task, trial, llm, untested=args.untested)
            rows += new
            print("  ".join(_line(r) for r in new))
            out.write_text(
                json.dumps(
                    {
                        "tag": args.tag,
                        "model": settings.llm_model,
                        "untested": args.untested,
                        "started": stamp,
                        "rows": rows,
                    },
                    indent=1,
                )
            )
    print()
    report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
