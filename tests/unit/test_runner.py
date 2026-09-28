from patchbay.agent.loop import Limits
from patchbay.db.engine import db_session
from patchbay.db.models import Message, Session
from patchbay.events import EV_DIFF, EV_SANDBOX_READY, EV_STATUS, EV_TOOL_CALL, EV_USAGE
from patchbay.jobs.runner import run_session
from patchbay.llm import FakeLLM
from patchbay.sandbox import FakeSandbox


def _session(prompt="do it", repo_url=None):
    with db_session() as db:
        s = Session(title=prompt[:60], prompt=prompt, repo_url=repo_url)
        db.add(s)
        db.flush()
        return s.id


def _factory(sb):
    calls = []

    def f(session_id, repo_url, container_id):
        calls.append((session_id, repo_url, container_id))
        sb.container_id = "cid-1"
        return sb

    f.calls = calls
    return f


def test_happy_path_records_everything(bus):
    sid = _session("add a flag")
    sb = FakeSandbox(responses={"pytest": "2 passed\n"})
    llm = FakeLLM.script(
        FakeLLM.tool_call("write_file", path="a.py", content="x=1\n"),
        FakeLLM.tool_call("bash", command="python -m pytest -q"),
        FakeLLM.tool_call("finish", summary="added a.py; 2 passed"),
    )
    status = run_session(sid, llm=llm, bus=bus, sandbox_factory=_factory(sb))
    assert status == "completed"

    with db_session() as db:
        s = db.get(Session, sid)
        assert s.status == "completed" and s.summary == "added a.py; 2 passed"
        assert s.steps == 3 and s.prompt_tokens == 30 and s.completion_tokens == 15
        assert s.container_id == "cid-1" and s.last_run_id
        roles = [m.role for m in s.messages]
        # system, user, then (assistant, tool) × 3
        assert roles == ["system", "user"] + ["assistant", "tool"] * 3
        assert [m.seq for m in s.messages] == list(range(8))
        assert "add a flag" == s.messages[1].payload["content"]
        assert "/workspace" in s.messages[0].payload["content"]

    types = [e["type"] for e in bus.replay(sid)]
    assert types[0] == EV_STATUS and types[1] == EV_SANDBOX_READY
    assert types.count(EV_TOOL_CALL) == 3 and types.count(EV_USAGE) == 3
    # diff.updated after write_file and bash, not after finish
    diffs = [e["payload"] for e in bus.replay(sid) if e["type"] == EV_DIFF]
    assert len(diffs) == 2 and diffs[-1] == {"files": 1, "insertions": 1, "deletions": 0}
    with db_session() as db:
        s = db.get(Session, sid)
        assert s.patch and "+x=1" in s.patch and s.diff_files == 1 and s.diff_insertions == 1
    assert types[-1] == EV_STATUS and bus.replay(sid)[-1]["payload"]["status"] == "completed"
    assert bus.acquire_lock(sid, "x") is not False  # lock released


def test_follow_up_resumes_transcript_and_reuses_container(bus):
    sid = _session("first task")
    sb = FakeSandbox()
    first = FakeLLM.script(FakeLLM.tool_call("ask_user", question="which db?"))
    assert run_session(sid, llm=first, bus=bus, sandbox_factory=_factory(sb)) == "waiting_for_user"
    with db_session() as db:
        s = db.get(Session, sid)
        assert s.status == "waiting_for_user" and s.summary == "which db?"
        n = len(s.messages)
        db.add(
            Message(
                session_id=sid, seq=n, role="user", payload={"role": "user", "content": "sqlite"}
            )
        )
        s.status = "queued"  # what POST /messages does before enqueuing

    second = FakeLLM.script(FakeLLM.tool_call("finish", summary="done with sqlite"))
    factory = _factory(sb)
    assert run_session(sid, llm=second, bus=bus, sandbox_factory=factory) == "completed"
    assert factory.calls[0][2] == "cid-1"  # asked to attach the existing container
    # the second run saw the whole history, ending with the user's answer
    sent = second.requests[0]["messages"]
    assert sent[0]["role"] == "system" and sent[-1] == {"role": "user", "content": "sqlite"}
    assert len(sent) == n + 1
    with db_session() as db:
        s = db.get(Session, sid)
        assert s.steps == 2 and s.status == "completed"
        assert [m.seq for m in s.messages] == list(range(len(s.messages)))


def test_cancel_before_start_and_lock_prevents_double_run(bus):
    sid = _session()
    bus.request_cancel(sid)
    assert (
        run_session(sid, llm=FakeLLM.script(), bus=bus, sandbox_factory=_factory(FakeSandbox()))
        == "cancelled"
    )
    with db_session() as db:
        assert db.get(Session, sid).status == "cancelled"

    sid2 = _session()
    assert bus.acquire_lock(sid2, "other-worker")
    assert (
        run_session(sid2, llm=FakeLLM.script(), bus=bus, sandbox_factory=_factory(FakeSandbox()))
        == "locked"
    )
    with db_session() as db:
        assert db.get(Session, sid2).status == "queued"  # untouched


def test_crash_is_recorded_as_failed_not_raised(bus):
    sid = _session()

    def boom(session_id, repo_url, container_id):
        raise RuntimeError("docker is down")

    assert run_session(sid, llm=FakeLLM.script(), bus=bus, sandbox_factory=boom) == "failed"
    with db_session() as db:
        s = db.get(Session, sid)
        assert s.status == "failed" and "docker is down" in s.error
    assert bus.replay(sid)[-1]["payload"] == {
        "status": "failed",
        "error": "RuntimeError: docker is down",
    }
    assert bus.acquire_lock(sid, "x")  # lock was released


def test_max_steps_and_missing_session(bus):
    sid = _session()
    llm = FakeLLM.script(*[FakeLLM.tool_call("bash", command="ls")] * 5)
    status = run_session(
        sid, llm=llm, bus=bus, sandbox_factory=_factory(FakeSandbox()), limits=Limits(max_steps=2)
    )
    assert status == "failed"
    with db_session() as db:
        assert "max steps" in db.get(Session, sid).error
    assert (
        run_session("s_nope", llm=llm, bus=bus, sandbox_factory=_factory(FakeSandbox()))
        == "missing"
    )


def test_stale_redelivery_of_a_finished_session_is_ignored(bus):
    """Celery redelivers after its visibility timeout; a session that is already
    finished (and has no new user message) must not be run again."""
    sid = _session()
    with db_session() as db:
        db.get(Session, sid).status = "completed"
    llm = FakeLLM.script(FakeLLM.tool_call("finish", summary="should not run"))
    assert run_session(sid, llm=llm, bus=bus, sandbox_factory=_factory(FakeSandbox())) == "stale"
    assert llm.requests == []


def test_lease_is_renewed_while_the_run_is_alive(bus, monkeypatch):
    from patchbay.jobs import runner as runner_mod

    renewed = []
    real_renew = bus.renew_lock
    monkeypatch.setattr(
        bus,
        "renew_lock",
        lambda sid, owner, ttl=None: (renewed.append(owner), real_renew(sid, owner, ttl))[1],
    )
    monkeypatch.setattr(
        runner_mod, "_renew_lease", lambda b, sid, owner, stop: b.renew_lock(sid, owner)
    )
    sid = _session()
    llm = FakeLLM.script(FakeLLM.tool_call("finish", summary="done"))
    assert (
        run_session(sid, llm=llm, bus=bus, sandbox_factory=_factory(FakeSandbox())) == "completed"
    )
    assert renewed  # the heartbeat ran at least once


def test_resume_after_crash_repairs_the_transcript_before_calling_the_model(bus):
    """A worker died between saving an assistant tool call and saving its result."""
    from patchbay.agent.loop import INTERRUPTED_NOTE

    sid = _session()
    crash_shaped = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": "{}"},
                }
            ],
        },
    ]
    with db_session() as db:
        for i, m in enumerate(crash_shaped):
            db.add(Message(session_id=sid, seq=i, role=m["role"], payload=m))
        db.get(Session, sid).status = "queued"  # what the sweeper does
    llm = FakeLLM.script(FakeLLM.tool_call("finish", summary="checked and done"))
    assert (
        run_session(sid, llm=llm, bus=bus, sandbox_factory=_factory(FakeSandbox())) == "completed"
    )
    sent = llm.requests[0]["messages"]
    assert sent[-1] == {"role": "tool", "tool_call_id": "call_x", "content": INTERRUPTED_NOTE}
    with db_session() as db:
        s = db.get(Session, sid)
        assert [m.seq for m in s.messages] == list(range(len(s.messages)))
        assert s.messages[3].payload["tool_call_id"] == "call_x"


def test_usage_is_recorded_per_turn_so_a_hard_crash_does_not_lose_it(bus):
    """A process being torn down skips the end-of-run bookkeeping; spend must
    already be on the row (it counts against the session's token budget)."""

    class Crash(BaseException):
        pass

    class DiesOnThirdCall:
        model = "fake"

        def __init__(self):
            self.inner = FakeLLM.script(*[FakeLLM.tool_call("bash", command="ls")] * 2)
            self.n = 0

        def chat(self, messages, tools=None):
            self.n += 1
            if self.n == 3:
                raise Crash()
            return self.inner.chat(messages, tools)

    sid = _session()
    try:
        run_session(sid, llm=DiesOnThirdCall(), bus=bus, sandbox_factory=_factory(FakeSandbox()))
    except Crash:
        pass
    with db_session() as db:
        s = db.get(Session, sid)
        assert s.steps == 2 and s.prompt_tokens == 20 and s.completion_tokens == 10
        assert s.status == "running"  # left for the sweeper, as a dead worker would
    assert bus.acquire_lock(sid, "next-worker")  # lease released by finally


def test_the_project_is_saved_as_a_zip_when_a_run_ends(bus, projects_dir):
    import io
    import zipfile

    sid = _session("make it")
    sb = FakeSandbox()
    llm = FakeLLM.script(
        FakeLLM.tool_call("write_file", path="src/app.js", content="export {}\n"),
        FakeLLM.tool_call("write_file", path="node_modules/x/index.js", content="lib"),
        FakeLLM.tool_call("finish", summary="done"),
    )
    assert run_session(sid, llm=llm, bus=bus, sandbox_factory=_factory(sb)) == "completed"
    names = zipfile.ZipFile(io.BytesIO((projects_dir / f"{sid}.zip").read_bytes())).namelist()
    assert names == [f"{sid}/src/app.js"]
