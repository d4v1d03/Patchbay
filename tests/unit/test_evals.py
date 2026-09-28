import json

from evals.prices import cost_usd
from evals.report import summarise, totals
from evals.tasks import Task, load_tasks


def test_all_task_files_load_and_are_well_formed():
    tasks = load_tasks()
    names = [t.name for t in tasks]
    assert len(tasks) >= 6 and len(set(names)) == len(names)
    for t in tasks:
        assert t.prompt.strip()
        if t.expect != "ask_user":
            assert t.check and t.hidden, t.name
        if t.kind == "repo":
            assert t.repo_url


def test_pass_rules():
    t = Task(name="x", prompt="p", check="true", hidden={"a": "b"})
    assert t.passed("completed", 0)
    assert not t.passed("completed", 1)
    assert not t.passed("failed", 0)
    ask = Task(name="y", prompt="p", expect="ask_user")
    assert ask.passed("waiting_for_user", None)
    assert not ask.passed("completed", 0)


def test_cost_table():
    assert cost_usd("deepseek-flash", 1_000_000, 0) == 0.15
    # a fully cached prompt costs 2% of an uncached one on deepseek-flash
    assert cost_usd("deepseek-flash", 1_000_000, 0, 1_000_000) == 0.003
    assert cost_usd("deepseek-chat", 1_000_000, 0) == 0.15  # legacy alias
    assert (
        abs(cost_usd("gemini-3.8-flash", 1_000_000, 1_000_000, 500_000) - (0.15 + 0.0375 + 2.5))
        < 1e-9
    )
    assert cost_usd("unknown-model", 100, 100) is None


def _r(task, passed, steps=5, infra=False, cost=0.01):
    return {
        "task": task,
        "passed": passed,
        "steps": steps,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cached_tokens": 200,
        "cost_usd": cost,
        "seconds": 30.0,
        "infra_error": infra,
    }


def test_summarise_pass_at_1_and_k_and_infra_exclusion():
    payload = {
        "results": [_r("a", True), _r("a", False), _r("a", False, infra=True), _r("b", False)]
    }
    s = summarise(payload)
    assert s["a"]["trials"] == 3 and s["a"]["infra_errors"] == 1
    assert s["a"]["pass@1"] == 0.5  # infra error excluded from the denominator
    assert s["a"]["pass@k"] == 1.0
    assert s["b"]["pass@1"] == 0.0 and s["b"]["pass@k"] == 0.0
    t = totals(s)
    assert t["pass@1"] == 0.25 and t["trials"] == 4
    assert abs(s["a"]["cached%"] - 20.0) < 1e-9
    json.dumps(s)  # serialisable


def test_compare_totals_use_shared_tasks_only(capsys):
    from evals.report import print_compare

    def run(tag, rows):
        return {
            "tag": tag,
            "results": [
                {
                    "task": t,
                    "trial": 1,
                    "model": "m",
                    "prompt_version": "v",
                    "status": "completed",
                    "passed": True,
                    "steps": steps,
                    "tool_calls": steps,
                    "prompt_tokens": tok,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cost_usd": 0.0,
                    "seconds": 1.0,
                    "check_exit": 0,
                    "infra_error": False,
                    "error": None,
                    "summary": None,
                    "run_id": None,
                }
                for t, steps, tok in rows
            ],
        }

    a = run("a", [("short", 2, 1000)])
    b = run("b", [("short", 2, 1000), ("long", 20, 90000)])  # b has an extra, expensive task
    print_compare(a, b)
    out = capsys.readouterr().out
    assert "excluded: long" in out
    tokens_row = next(line for line in out.splitlines() if line.startswith("ALL tokens"))
    assert tokens_row.split()[-1] == "+0%"  # not skewed by the task only b ran


def test_wilson_interval_is_honest_at_small_n():
    from evals.report import wilson

    lo, hi = wilson(3, 3)
    assert hi == 1.0 and lo < 0.45  # 3/3 is compatible with a true rate well under 50%
    lo, hi = wilson(95, 100)
    assert 0.88 < lo < 0.95 < hi < 0.99
    assert wilson(0, 0) == (0.0, 1.0)


def test_seed_commits_setup_files_as_the_baseline():
    from patchbay.sandbox import FakeSandbox

    sb = FakeSandbox()
    Task(name="x", prompt="p", setup={"a.py": "x = 1\n"}).seed(sb)
    assert sb.read_file("a.py") == "x = 1\n"
    assert any("git add -A" in c and "commit" in c for c in sb.commands)
