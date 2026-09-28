import json

import pytest
from fastapi.testclient import TestClient

from patchbay import main
from patchbay.db.engine import db_session
from patchbay.db.models import Message, Session
from patchbay.events import EV_STATUS, EV_TOOL_CALL
from patchbay.web import api


@pytest.fixture
def client(bus, monkeypatch):
    queued = []
    monkeypatch.setattr(api, "enqueue_run", lambda sid: queued.append(sid))
    c = TestClient(main.app)
    c.queued = queued
    c.bus = bus
    return c


def test_create_json_lists_and_gets(client):
    r = client.post(
        "/api/sessions", json={"prompt": "Add a --version flag\nand a test", "repo_url": ""}
    )
    assert r.status_code == 201
    s = r.json()
    assert (
        s["status"] == "queued" and s["title"] == "Add a --version flag" and s["repo_url"] is None
    )
    assert client.queued == [s["id"]]
    assert [e["type"] for e in client.bus.replay(s["id"])] == ["user.message", EV_STATUS]

    assert client.get("/api/sessions").json()[0]["id"] == s["id"]
    assert (
        client.get(f"/api/sessions/{s['id']}").json()["prompt"]
        == "Add a --version flag\nand a test"
    )
    assert client.get("/api/sessions/s_nope").status_code == 404


def test_create_form_redirects_and_validates_repo(client):
    r = client.post(
        "/api/sessions",
        data={"prompt": "hi", "repo_url": "https://github.com/x/y"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/sessions/s_")
    r = client.post("/api/sessions", json={"prompt": "hi", "repo_url": "git@github.com:x/y.git"})
    assert r.status_code == 400
    r = client.post("/api/sessions", json={"prompt": ""})
    assert r.status_code == 422


def test_stop_queued_and_running(client):
    sid = client.post("/api/sessions", json={"prompt": "x"}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/stop").json() == {
        "status": "cancelled",
        "stopped": True,
    }
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "cancelled"
    assert client.post(f"/api/sessions/{sid}/stop").json()["stopped"] is False  # already terminal

    sid2 = client.post("/api/sessions", json={"prompt": "y"}).json()["id"]
    with db_session() as db:
        db.get(Session, sid2).status = "running"
    r = client.post(f"/api/sessions/{sid2}/stop").json()
    assert r["status"] == "running" and r["stopped"] is True
    assert client.bus.is_cancelled(sid2)


def test_follow_up_requeues_only_when_idle(client):
    sid = client.post("/api/sessions", json={"prompt": "first"}).json()["id"]
    assert (
        client.post(f"/api/sessions/{sid}/messages", json={"content": "more"}).status_code == 409
    )  # queued
    with db_session() as db:
        db.get(Session, sid).status = "waiting_for_user"
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "use sqlite"})
    assert r.status_code == 200 and r.json() == {"status": "queued", "seq": 0}
    assert client.queued == [sid, sid]
    assert client.get(f"/api/sessions/{sid}/messages").json() == [
        {"role": "user", "content": "use sqlite"}
    ]


def _sse_events(resp):
    out, cur = [], {}
    for line in resp.iter_lines():
        if not line:
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(":")
        cur[k] = v.strip()
    return out


def test_sse_replays_then_closes_on_terminal_status(client):
    sid = client.post("/api/sessions", json={"prompt": "x"}).json()["id"]
    client.bus.publish(
        sid, EV_TOOL_CALL, {"call_id": "c1", "name": "bash", "arguments": {"command": "ls"}}
    )
    client.bus.publish(sid, EV_STATUS, {"status": "completed"})
    with client.stream("GET", f"/api/sessions/{sid}/events") as resp:
        assert resp.status_code == 200 and resp.headers["content-type"].startswith(
            "text/event-stream"
        )
        frames = _sse_events(resp)
    kinds = [f["event"] for f in frames]
    assert kinds == ["user.message", "session.status", "agent.tool_call", "session.status"]
    assert json.loads(frames[2]["data"])["payload"]["name"] == "bash"
    assert [int(f["id"]) for f in frames] == sorted(int(f["id"]) for f in frames)

    # resume from the second frame's id → only the rest
    with client.stream(
        "GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": frames[1]["id"]}
    ) as resp:
        again = _sse_events(resp)
    assert [f["event"] for f in again] == ["agent.tool_call", "session.status"]
    assert client.get("/api/sessions/s_nope/events").status_code == 404


def test_pages_render(client):
    sid = client.post("/api/sessions", json={"prompt": "render me"}).json()["id"]
    assert "render me" in client.get("/").text
    page = client.get(f"/sessions/{sid}")
    assert page.status_code == 200 and "user.message" in page.text and "session.js" in page.text
    assert client.get("/static/session.js").status_code == 200
    assert client.get("/sessions/s_nope").status_code == 404


def test_trace_step_endpoint(client, tmp_path, monkeypatch):
    from patchbay import trace as trace_mod
    from patchbay.llm.demo import DemoLLM
    from patchbay.llm.trace import TraceWriter
    from patchbay.llm.types import system_message, user_message

    monkeypatch.setattr(trace_mod, "TRACES_DIR", tmp_path)
    llm = DemoLLM(delay_s=0, trace=TraceWriter("run-x", tmp_path))
    msgs = [system_message("SYS"), user_message("make hello")]
    t1 = llm.chat(msgs)
    msgs += [t1.to_message(), {"role": "tool", "tool_call_id": t1.tool_calls[0].id, "content": "."}]
    llm.chat(msgs)

    r = client.get("/api/traces/run-x/steps/2")
    assert r.status_code == 200
    d = r.json()
    assert d["system_prompt"] == "SYS" and d["total_messages"] == 4
    assert [m["role"] for m in d["new_messages"]] == ["assistant", "tool"]
    assert d["response"]["tool_calls"][0]["name"] == "write_file"
    assert client.get("/api/traces/run-x/steps/9").status_code == 404
    assert client.get("/api/traces/nope/steps/1").status_code == 404


def test_diff_endpoint_prefers_live_container_then_stored_patch(client, monkeypatch):
    from patchbay.web import api as api_mod

    sid = client.post("/api/sessions", json={"prompt": "x"}).json()["id"]
    with db_session() as db:
        s = db.get(Session, sid)
        s.patch, s.diff_files, s.diff_insertions, s.container_id = (
            "diff --git a/a b/a\n+hi\n",
            1,
            1,
            "cid-gone",
        )

    # container gone → stored patch
    monkeypatch.setattr(api_mod, "_live_diff", lambda cid: None)
    d = client.get(f"/api/sessions/{sid}/diff").json()
    assert d["live"] is False and "+hi" in d["patch"] and d["stats"]["files"] == 1

    # container alive → live diff wins
    monkeypatch.setattr(
        api_mod,
        "_live_diff",
        lambda cid: ("diff --git a/b b/b\n+live\n", {"files": 2, "insertions": 5, "deletions": 0}),
    )
    d = client.get(f"/api/sessions/{sid}/diff").json()
    assert d["live"] is True and "+live" in d["patch"] and d["stats"]["insertions"] == 5

    r = client.get(f"/sessions/{sid}/diff.patch")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-patch")
    assert r.headers["content-disposition"] == f'attachment; filename="{sid}.patch"'
    assert r.text.startswith("diff --git")
    assert client.get("/api/sessions/s_nope/diff").status_code == 404


def test_project_zip_download(client, projects_dir):
    sid = client.post("/api/sessions", json={"prompt": "zip it"}).json()["id"]
    assert client.get(f"/sessions/{sid}/project.zip").status_code == 404
    assert 'id="project"' in client.get(f"/sessions/{sid}").text  # rendered, hidden
    projects_dir.mkdir(parents=True)
    (projects_dir / f"{sid}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    r = client.get(f"/sessions/{sid}/project.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f'filename="{sid}.zip"' in r.headers["content-disposition"]


def test_delete_session_removes_rows_stream_and_traces(client, tmp_path, monkeypatch):
    from patchbay import trace as trace_mod
    from patchbay.web import api as api_mod

    monkeypatch.setattr(trace_mod, "TRACES_DIR", tmp_path)
    destroyed = []
    monkeypatch.setattr(api_mod, "destroy_container", destroyed.append)

    sid = client.post("/api/sessions", json={"prompt": "bye"}).json()["id"]
    (tmp_path / "run-1.jsonl").write_text("{}\n")
    with db_session() as db:
        s = db.get(Session, sid)
        s.status, s.container_id, s.last_run_id = "completed", "cid-9", "run-1"
        db.add(
            Message(session_id=sid, seq=0, role="user", payload={"role": "user", "content": "bye"})
        )
    client.bus.publish(sid, "llm.usage", {"run_id": "run-1", "step": 1})

    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    with db_session() as db:
        assert db.query(Message).filter(Message.session_id == sid).count() == 0
    assert client.bus.replay(sid) == [] and not client.bus.redis.exists(f"session:{sid}:events")
    assert destroyed == ["cid-9"] and not (tmp_path / "run-1.jsonl").exists()
    assert not (tmp_path / "projects" / f"{sid}.zip").exists()

    sid2 = client.post("/api/sessions", json={"prompt": "busy"}).json()["id"]
    with db_session() as db:
        db.get(Session, sid2).status = "running"
    assert client.delete(f"/api/sessions/{sid2}").status_code == 409
    assert client.delete("/api/sessions/s_nope").status_code == 404


def test_live_diff_tolerates_no_docker(monkeypatch):
    """web may run without a docker socket (compose): any docker failure → stored patch."""
    from patchbay.web import api as api_mod

    class Boom:
        @staticmethod
        def attach(cid):
            raise RuntimeError("Error while fetching server API version")

    monkeypatch.setattr(api_mod, "Sandbox", Boom)
    assert api_mod._live_diff("cid") is None
    api_mod.destroy_container("cid")  # must not raise
