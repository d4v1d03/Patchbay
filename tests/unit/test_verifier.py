import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from patchbay.agent.loop import EV_REVIEW, EV_USAGE, Limits, run_agent
from patchbay.agent.tools import default_registry
from patchbay.agent.verifier import Verdict, Verifier, gather_evidence, last_command
from patchbay.llm import FakeLLM, system_message, user_message
from patchbay.llm.client import parse_response, thinking_params
from patchbay.llm.structured import StructuredOutputError, chat_json
from patchbay.sandbox import FakeSandbox


def verdict(approve, *issues):
    return FakeLLM.text(json.dumps({"approve": approve, "issues": list(issues)}))


class Recorder(list):
    def __call__(self, kind, payload):
        self.append((kind, payload))

    def of(self, kind):
        return [p for k, p in self if k == kind]


def run(agent_llm, reviewer_llm, *, rejections=2, limits=None):
    transcript = [system_message("sys"), user_message("make take() return n items")]
    rec = Recorder()
    outcome = run_agent(
        llm=agent_llm,
        sandbox=FakeSandbox(files={"a.py": "x = 1\n"}, responses={"pytest": "1 passed\n"}),
        transcript=transcript,
        tools=default_registry(),
        emit=rec,
        limits=limits or Limits(max_steps=10),
        verifier=Verifier(reviewer_llm, max_rejections=rejections),
    )
    return outcome, transcript, rec


# ---------------------------------------------------------------- structured output


def test_chat_json_sends_the_validation_error_back_once():
    llm = FakeLLM.script(FakeLLM.text('{"approve": "maybe"}'), verdict(True))
    v, usage = chat_json(llm, [user_message("review, json")], Verdict)
    assert v.approve is True
    assert usage.prompt_tokens == 20  # both attempts are paid for
    retry = llm.requests[1]["messages"]
    assert retry[-1]["role"] == "user" and "approve" in retry[-1]["content"]
    assert llm.requests[0]["response_format"] == {"type": "json_object"}


def test_chat_json_gives_up_with_the_usage_attached():
    llm = FakeLLM.script(FakeLLM.text("not json"), FakeLLM.text("{}"))
    with pytest.raises(StructuredOutputError) as e:
        chat_json(llm, [user_message("json")], Verdict)
    assert e.value.usage.prompt_tokens == 20


def test_a_rejection_must_say_why():
    with pytest.raises(ValidationError):
        Verdict(approve=False, issues=[])


# ---------------------------------------------------------------- in the loop


def test_rejected_finish_goes_back_to_the_agent_then_approved():
    agent = FakeLLM.script(
        FakeLLM.tool_call("bash", command="python3 -m pytest -q"),
        FakeLLM.tool_call("finish", summary="done"),
        FakeLLM.tool_call("write_file", path="a.py", content="x = 2\n"),
        FakeLLM.tool_call("finish", summary="done, fixed the edge case"),
    )
    reviewer = FakeLLM.script(verdict(False, "take(x, 0) returns one item"), verdict(True))
    outcome, transcript, rec = run(agent, reviewer)

    assert outcome.status == "completed" and outcome.summary == "done, fixed the edge case"
    assert [r["approve"] for r in rec.of(EV_REVIEW)] == [False, True]
    rejected = next(m for m in transcript if "Not accepted yet" in (m.get("content") or ""))
    assert rejected["role"] == "tool" and "take(x, 0)" in rejected["content"]
    # the reviewer saw the diff and the last test run
    seen = reviewer.requests[0]["messages"][1]["content"]
    assert "## Diff" in seen and "$ python3 -m pytest -q" in seen and "1 passed" in seen


def test_a_plain_reply_is_reviewed_like_finish():
    agent = FakeLLM.script(
        FakeLLM.text("All tests pass."),
        FakeLLM.tool_call("bash", command="python3 -m pytest -q"),
        FakeLLM.text("All tests pass, checked."),
    )
    reviewer = FakeLLM.script(verdict(False, "no tests were run"), verdict(True))
    outcome, transcript, rec = run(agent, reviewer)
    assert outcome.status == "completed" and outcome.summary == "All tests pass, checked."
    assert [r["approve"] for r in rec.of(EV_REVIEW)] == [False, True]
    sent_back = transcript[3]
    assert sent_back["role"] == "user" and "no tests were run" in sent_back["content"]


def test_after_max_rejections_the_next_finish_is_accepted_unreviewed():
    agent = FakeLLM.script(
        FakeLLM.tool_call("finish", summary="one"),
        FakeLLM.tool_call("finish", summary="two"),
    )
    reviewer = FakeLLM.script(verdict(False, "wrong"))  # a second review would exhaust it
    outcome, _, rec = run(agent, reviewer, rejections=1)
    assert outcome.status == "completed" and outcome.summary == "two"
    assert len(rec.of(EV_REVIEW)) == 1


def test_a_broken_review_lets_the_finish_stand():
    agent = FakeLLM.script(FakeLLM.tool_call("finish", summary="done"))
    reviewer = FakeLLM.script(FakeLLM.text("sure!"), FakeLLM.text("looks good"))
    outcome, _, rec = run(agent, reviewer)
    assert outcome.status == "completed"
    assert rec.of(EV_REVIEW)[0]["error"]


def test_review_tokens_count_against_the_budget_but_not_as_steps():
    agent = FakeLLM.script(FakeLLM.tool_call("finish", summary="done"))
    reviewer = FakeLLM.script(verdict(True))
    limits = Limits(max_steps=10)
    outcome, _, rec = run(agent, reviewer, limits=limits)
    assert outcome.steps == 1
    assert limits.tokens_used == 30  # 15 for the agent's turn + 15 for the review
    assert [u.get("role") for u in rec.of(EV_USAGE)] == [None, "verifier"]


def test_evidence_uses_the_latest_command_with_a_result():
    t = [
        user_message("task"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {"name": "bash", "arguments": '{"command": "ls"}'}},
                {"id": "b", "function": {"name": "bash", "arguments": '{"command": "pytest"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "a.py"},
        {"role": "tool", "tool_call_id": "b", "content": "2 failed"},
    ]
    assert last_command(t) == ("pytest", "2 failed")
    e = gather_evidence(t, "all good", FakeSandbox())
    assert e.request == "task" and e.last_output == "2 failed"


# ---------------------------------------------------------------- thinking


def test_thinking_levels_map_to_request_fields():
    assert thinking_params("") == {}
    assert thinking_params("off") == {"thinking": {"type": "disabled"}}
    assert thinking_params("High") == {"reasoning_effort": "high"}


def test_reasoning_tokens_are_parsed():
    usage = SimpleNamespace(
        prompt_tokens=50,
        completion_tokens=40,
        prompt_tokens_details=None,
        prompt_cache_hit_tokens=0,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=33),
    )
    msg = SimpleNamespace(content="x", tool_calls=[])
    r = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)
    assert parse_response(r, latency_ms=1, model="m").usage.reasoning_tokens == 33
