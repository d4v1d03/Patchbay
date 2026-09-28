from patchbay.agent.loop import run_agent
from patchbay.agent.tools import default_registry
from patchbay.llm.demo import DemoLLM
from patchbay.llm.types import system_message, user_message
from patchbay.sandbox import FakeSandbox


def _run(prompt, extra=()):
    transcript = [system_message("sys"), user_message(prompt), *extra]
    events = []
    out = run_agent(
        llm=DemoLLM(delay_s=0),
        sandbox=FakeSandbox(responses={"pytest": "1 passed\n"}),
        transcript=transcript,
        tools=default_registry(),
        emit=lambda k, p: events.append(k),
    )
    return out, transcript, events


def test_demo_completes_a_task():
    out, transcript, events = _run("make hello")
    assert out.status == "completed" and out.steps == 5 and "1 passed" in out.summary
    assert events.count("agent.tool_call") == 5


def test_demo_asks_when_prompted_and_answers_follow_up():
    out, transcript, _ = _run("please ask me something")
    assert out.status == "waiting_for_user" and "SQLite" in out.question
    transcript.append(user_message("SQLite"))
    out2 = run_agent(
        llm=DemoLLM(delay_s=0),
        sandbox=FakeSandbox(),
        transcript=transcript,
        tools=default_registry(),
        emit=lambda k, p: None,
    )
    assert out2.status == "completed" and out2.steps == 2
