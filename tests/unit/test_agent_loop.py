from patchbay.agent.loop import (
    EV_ASK_USER,
    EV_ERROR,
    EV_MESSAGE,
    EV_TOOL_CALL,
    EV_TOOL_RESULT,
    EV_USAGE,
    Limits,
    run_agent,
)
from patchbay.agent.tools import default_registry
from patchbay.llm import FakeLLM, system_message, user_message
from patchbay.sandbox import FakeSandbox


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.events]


def run(llm, sandbox=None, **kw):
    transcript = [system_message("sys"), user_message("do the thing")]
    rec = Recorder()
    outcome = run_agent(
        llm=llm,
        sandbox=sandbox or FakeSandbox(),
        transcript=transcript,
        tools=default_registry(),
        emit=rec,
        **kw,
    )
    return outcome, transcript, rec


def assert_transcript_valid(transcript):
    """Every assistant tool call is answered by exactly one tool message, in order."""
    pending = []
    for m in transcript:
        if m["role"] == "assistant":
            assert not pending, "new assistant turn while tool calls were unanswered"
            pending = [tc["id"] for tc in m.get("tool_calls", [])]
        elif m["role"] == "tool":
            assert pending and m["tool_call_id"] == pending.pop(0), "tool result out of order"
    assert not pending, f"dangling tool calls: {pending}"


def test_happy_path_explore_edit_test_finish():
    sb = FakeSandbox(files={"app.py": "x = 1\n"}, responses={"pytest": "1 passed\n"})
    llm = FakeLLM.script(
        FakeLLM.tool_call("list_files"),
        FakeLLM.tool_call("read_file", path="app.py"),
        FakeLLM.tool_call("write_file", path="app.py", content="x = 2\n"),
        FakeLLM.tool_call("bash", command="python -m pytest -q"),
        FakeLLM.tool_call("finish", summary="changed x to 2; tests pass"),
    )
    outcome, transcript, rec = run(llm, sb)
    assert outcome.status == "completed"
    assert outcome.summary == "changed x to 2; tests pass"
    assert outcome.steps == 5
    assert sb.read_file("app.py") == "x = 2\n"
    assert_transcript_valid(transcript)
    assert rec.kinds().count(EV_TOOL_CALL) == 5 and rec.kinds().count(EV_TOOL_RESULT) == 5
    assert rec.kinds().count(EV_USAGE) == 5
    # tool_call always precedes its tool_result
    assert rec.kinds().index(EV_TOOL_CALL) < rec.kinds().index(EV_TOOL_RESULT)


def test_text_without_tool_calls_completes():
    llm = FakeLLM.script(FakeLLM.text("All done, nothing to change."))
    outcome, transcript, rec = run(llm)
    assert outcome.status == "completed" and outcome.summary == "All done, nothing to change."
    assert (EV_MESSAGE, {"content": "All done, nothing to change."}) in rec.events
    assert transcript[-1]["role"] == "assistant"


def test_ask_user_pauses_with_question():
    llm = FakeLLM.script(FakeLLM.tool_call("ask_user", question="Postgres or SQLite?"))
    outcome, transcript, rec = run(llm)
    assert outcome.status == "waiting_for_user" and outcome.question == "Postgres or SQLite?"
    assert (EV_ASK_USER, {"question": "Postgres or SQLite?"}) in rec.events
    assert_transcript_valid(transcript)


def test_max_steps_fails_cleanly():
    llm = FakeLLM.script(*[FakeLLM.tool_call("bash", command="ls")] * 10)
    outcome, transcript, rec = run(llm, limits=Limits(max_steps=3))
    assert outcome.status == "failed" and outcome.steps == 3
    assert "max steps" in (outcome.error or "").lower()
    assert_transcript_valid(transcript)


def test_cancel_before_llm_call_and_between_tools():
    llm = FakeLLM.script(FakeLLM.tool_call("bash", command="ls"))
    outcome, _, _ = run(llm, should_cancel=lambda: True)
    assert outcome.status == "cancelled" and outcome.steps == 0

    calls = {"n": 0}

    def cancel_after_first_tool():
        calls["n"] += 1
        return calls["n"] > 2  # False for the llm check and the first tool, True after

    llm = FakeLLM.script(FakeLLM.tool_calls(("bash", {"command": "a"}), ("bash", {"command": "b"})))
    sb = FakeSandbox()
    outcome, transcript, _ = run(llm, sb, should_cancel=cancel_after_first_tool)
    assert outcome.status == "cancelled"
    assert sb.commands == ["a"]  # second tool never ran
    assert_transcript_valid(transcript)  # ...but its call was still answered


def test_unknown_tool_and_bad_args_are_reported_and_loop_continues():
    llm = FakeLLM.script(
        FakeLLM.tool_call("teleport", to="prod"),
        FakeLLM.tool_call("bash"),  # missing command
        FakeLLM.tool_call("finish", summary="ok"),
    )
    outcome, transcript, rec = run(llm)
    assert outcome.status == "completed" and outcome.steps == 3
    results = [p for k, p in rec.events if k == EV_TOOL_RESULT]
    assert results[0]["is_error"] and "unknown tool" in results[0]["output"]
    assert results[1]["is_error"] and "missing required" in results[1]["output"]
    assert_transcript_valid(transcript)


def test_llm_exception_becomes_failed_outcome():
    class Boom:
        model = "boom"

        def chat(self, messages, tools=None):
            raise RuntimeError("provider is down")

    outcome, transcript, rec = run(Boom())
    assert outcome.status == "failed" and "provider is down" in (outcome.error or "")
    assert any(k == EV_ERROR for k, _ in rec.events)


def test_multiple_tool_calls_in_one_turn_run_in_order():
    sb = FakeSandbox()
    llm = FakeLLM.script(
        FakeLLM.tool_calls(
            ("bash", {"command": "one"}), ("bash", {"command": "two"}), content="doing both"
        ),
        FakeLLM.tool_call("finish", summary="done"),
    )
    outcome, transcript, rec = run(llm, sb)
    assert sb.commands == ["one", "two"]
    assert (EV_MESSAGE, {"content": "doing both"}) in rec.events
    assert_transcript_valid(transcript)
    assert outcome.steps == 2


def test_session_token_budget_stops_the_run():
    llm = FakeLLM.script(*[FakeLLM.tool_call("bash", command="ls")] * 5)  # 15 tokens per turn
    outcome, transcript, rec = run(
        llm, limits=Limits(max_steps=10, max_session_tokens=40, tokens_used=20)
    )
    # 20+15 = 35 after step 1; step 2 is paid for, so its calls run (50); step 3 never starts
    assert outcome.status == "failed" and outcome.steps == 2
    assert "token budget" in outcome.error
    assert_transcript_valid(transcript)
    assert any(k == EV_ERROR for k, _ in rec.events)


def test_the_turn_that_crosses_the_budget_can_still_finish():
    llm = FakeLLM.script(FakeLLM.tool_call("finish", summary="done"))
    outcome, _, _ = run(llm, limits=Limits(max_steps=10, max_session_tokens=30, tokens_used=20))
    assert outcome.status == "completed" and outcome.summary == "done"


def test_cached_tokens_count_a_tenth_against_the_budget():
    from patchbay.llm.types import Usage

    # the session that hit the old limit: 405k prompt (385k cached) + 9k out
    assert Usage(405_474, 8_778, 385_536).budget_tokens == 19_938 + 8_778 + 38_553
    assert Usage(100, 20).budget_tokens == 120


def test_budget_zero_means_unlimited():
    llm = FakeLLM.script(FakeLLM.tool_call("finish", summary="ok"))
    outcome, _, _ = run(llm, limits=Limits(max_session_tokens=0, tokens_used=10**9))
    assert outcome.status == "completed"


# ---- crash repair ---------------------------------------------------------------------


def _asst(*ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            for i in ids
        ],
    }


def test_repair_answers_calls_left_dangling_by_a_crash():
    from patchbay.agent.loop import INTERRUPTED_NOTE, repair_transcript

    t = [
        system_message("s"),
        user_message("u"),
        _asst("a"),
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        _asst("b", "c"),
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    added = repair_transcript(t)
    assert [m["tool_call_id"] for m in added] == ["c"]  # only the unanswered one
    assert t[-1] == {"role": "tool", "tool_call_id": "c", "content": INTERRUPTED_NOTE}
    assert_transcript_valid(t)
    assert repair_transcript(t) == []  # idempotent


def test_repair_is_a_noop_on_clean_transcripts():
    from patchbay.agent.loop import repair_transcript

    assert repair_transcript([system_message("s"), user_message("u")]) == []
    t = [
        system_message("s"),
        user_message("u"),
        _asst("a"),
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert repair_transcript(t) == [] and len(t) == 4


def test_agent_package_has_no_infrastructure_imports():
    import subprocess
    import sys

    code = (
        "import sys, patchbay.agent.loop, patchbay.agent.tools, patchbay.agent.prompts\n"
        "infra = ('sqlalchemy', 'redis', 'celery', 'patchbay.events.bus', 'patchbay.jobs')\n"
        "bad = [m for m in infra if m in sys.modules]\n"
        "assert not bad, bad"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_context_manager_thins_what_the_model_sees_not_the_transcript():
    from patchbay.agent.context import ContextManager, ContextPolicy
    from patchbay.events import EV_CONTEXT

    big = "x" * 4000
    sb = FakeSandbox(responses={"cat": big})
    llm = FakeLLM.script(
        *[FakeLLM.tool_call("bash", command=f"cat f{i}") for i in range(6)],
        FakeLLM.tool_call("finish", summary="done"),
    )
    ctx = ContextManager(ContextPolicy(budget_tokens=2500, keep_recent_turns=2))
    outcome, transcript, rec = run(llm, sb, context=ctx)
    assert outcome.status == "completed"

    # the stored transcript kept every output in full
    tool_msgs = [m for m in transcript if m["role"] == "tool"][:6]
    assert all(big in m["content"] for m in tool_msgs)
    # ...while a later request to the model carried stubs for the old ones
    last_request = llm.requests[-1]["messages"]
    assert any("chars elided" in (m.get("content") or "") for m in last_request)
    assert any(k == EV_CONTEXT for k, _ in rec.events)
    assert_transcript_valid(last_request)


def test_prompt_layouts_legacy_vs_static_system_prompt():
    """v0/v1 put session details in the system prompt; v2 keeps it static and
    puts them in the first user message, so a new session shares the whole
    system prompt + tool definitions prefix with every other session."""
    from patchbay.agent.prompts import build_initial_messages

    a = build_initial_messages(task="do X", tree="./a.py", repo_url="https://x/y", version="v1")
    b = build_initial_messages(task="do Y", tree="./b.py", repo_url=None, version="v1")
    assert a[0]["content"] != b[0]["content"] and a[1]["content"] == "do X"

    a = build_initial_messages(task="do X", tree="./a.py", repo_url="https://x/y", version="v2")
    b = build_initial_messages(task="do Y", tree="./b.py", repo_url=None, version="v2")
    assert a[0] == b[0]  # byte-identical system prompt across sessions
    assert "./a.py" in a[1]["content"] and a[1]["content"].endswith("## Task\ndo X")
    assert "cloned from https://x/y" in a[1]["content"] and "./b.py" in b[1]["content"]
