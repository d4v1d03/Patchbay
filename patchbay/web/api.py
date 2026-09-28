from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from patchbay.config import get_settings
from patchbay.db.engine import db_session
from patchbay.db.models import TERMINAL, Message, Session
from patchbay.events import EV_STATUS, EV_USER_MESSAGE
from patchbay.events.bus import get_bus
from patchbay.jobs.queue import enqueue_run
from patchbay.sandbox import Sandbox

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class CreateSession(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    repo_url: str | None = None


class FollowUp(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


def _title(prompt: str) -> str:
    first = prompt.strip().splitlines()[0]
    return first[:77] + "..." if len(first) > 80 else first


@router.post("/sessions")
async def create_session(request: Request):
    wants_json = request.headers.get("content-type", "").startswith("application/json")
    try:
        if wants_json:
            body = CreateSession.model_validate(await request.json())
        else:
            form = await request.form()
            body = CreateSession(
                prompt=str(form.get("prompt", "")), repo_url=str(form.get("repo_url") or "") or None
            )
    except ValidationError as e:
        raise HTTPException(422, e.errors()) from e
    repo_url = (body.repo_url or "").strip() or None
    if repo_url and not repo_url.startswith(("https://", "http://")):
        raise HTTPException(400, "repo_url must be an http(s) URL")

    with db_session() as db:
        sess = Session(
            title=_title(body.prompt),
            prompt=body.prompt.strip(),
            repo_url=repo_url,
            model=get_settings().llm_model,
            prompt_version=get_settings().prompt_version,
        )
        db.add(sess)
        db.flush()
        sid = sess.id
    bus = get_bus()
    bus.publish(sid, EV_USER_MESSAGE, {"content": body.prompt.strip()})
    bus.publish(sid, EV_STATUS, {"status": "queued"})
    enqueue_run(sid)

    if not wants_json:
        return RedirectResponse(f"/sessions/{sid}", status_code=303)
    with db_session() as db:
        return JSONResponse(db.get(Session, sid).to_dict(), status_code=201)


@router.get("/sessions")
def list_sessions(limit: int = 50):
    with db_session() as db:
        rows = db.query(Session).order_by(Session.created_at.desc()).limit(min(limit, 200)).all()
        return [s.to_dict() for s in rows]


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        return sess.to_dict()


@router.get("/sessions/{session_id}/messages")
def get_transcript(session_id: str):
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        return [m.payload for m in sess.messages]


@router.post("/sessions/{session_id}/stop")
def stop_session(session_id: str):
    bus = get_bus()
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        if sess.status in TERMINAL:
            return {"status": sess.status, "stopped": False}
        was_queued = sess.status == "queued"
        if was_queued:
            sess.status = "cancelled"
    if was_queued:
        bus.publish(session_id, EV_STATUS, {"status": "cancelled"})
        return {"status": "cancelled", "stopped": True}
    bus.request_cancel(session_id)  # the worker checks this between steps
    return {"status": "running", "stopped": True, "note": "cancelling after the current step"}


@router.post("/sessions/{session_id}/messages")
def follow_up(session_id: str, body: FollowUp):
    content = body.content.strip()
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        if sess.status in ("queued", "running"):
            raise HTTPException(409, f"session is {sess.status}; wait for it to finish")
        seq = len(sess.messages)
        db.add(
            Message(
                session_id=session_id,
                seq=seq,
                role="user",
                payload={"role": "user", "content": content},
            )
        )
        sess.status = "queued"
        sess.error = None
    bus = get_bus()
    bus.publish(session_id, EV_USER_MESSAGE, {"content": content})
    bus.publish(session_id, EV_STATUS, {"status": "queued"})
    enqueue_run(session_id)
    return {"status": "queued", "seq": seq}


@router.get("/traces/{run_id}/steps/{step}")
def trace_step(run_id: str, step: int):
    """What the model saw at one step: the messages new since the previous
    step, the system prompt length, and its reply."""
    from patchbay.trace import TRACES_DIR

    path = TRACES_DIR / f"{run_id}.jsonl"
    if not path.exists() or "/" in run_id or ".." in run_id:
        raise HTTPException(404, "no such trace")
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    cur = next((x for x in lines if x["step"] == step), None)
    if cur is None:
        raise HTTPException(404, f"no step {step}")
    prev = next((x for x in lines if x["step"] == step - 1), None)
    msgs = cur["request"]["messages"]
    start = len(prev["request"]["messages"]) if prev else 0
    return {
        "run_id": run_id,
        "step": step,
        "model": cur["model"],
        "total_messages": len(msgs),
        "system_prompt": msgs[0]["content"] if msgs and msgs[0]["role"] == "system" else None,
        "new_messages": msgs[start:],
        "tools": cur["request"]["tools"],
        "response": cur["response"],
        "usage": cur["usage"],
        "latency_ms": cur["latency_ms"],
        "error": cur["error"],
    }


def _live_diff(container_id: str | None) -> tuple[str, dict[str, int]] | None:
    if not container_id:
        return None
    try:
        sb = Sandbox.attach(container_id)
        return sb.diff(), sb.diff_stat()
    except Exception:  # noqa: BLE001 — no socket, container gone, docker down: use the stored patch
        return None


def get_diff_data(session_id: str) -> dict:
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        stored = sess.patch or ""
        stats = sess.to_dict()["diff"]
        container_id = sess.container_id
        running = sess.status == "running"
    live = _live_diff(container_id)
    if live is not None:
        patch, stats = live
        return {"patch": patch, "stats": stats, "live": True, "running": running}
    return {"patch": stored, "stats": stats, "live": False, "running": running}


@router.get("/sessions/{session_id}/diff")
def session_diff(session_id: str):
    return get_diff_data(session_id)


def destroy_container(container_id: str | None) -> None:
    if not container_id:
        return
    try:
        Sandbox.attach(container_id).destroy()
    except Exception:  # noqa: BLE001 — the reaper will collect it if we can't reach docker
        log.warning("could not destroy container %s", container_id[:12], exc_info=True)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str):
    from patchbay.jobs.runner import project_zip_path
    from patchbay.trace import TRACES_DIR

    bus = get_bus()
    with db_session() as db:
        sess = db.get(Session, session_id)
        if sess is None:
            raise HTTPException(404, "no such session")
        if sess.status == "running":
            raise HTTPException(409, "stop the session before deleting it")
        container_id = sess.container_id
        run_ids = {sess.last_run_id} | {
            e.payload.get("run_id") for e in sess.events if e.type == "llm.usage"
        }
        db.delete(sess)  # cascades to messages and events
    destroy_container(container_id)
    bus.forget(session_id)
    project_zip_path(session_id).unlink(missing_ok=True)
    for run_id in run_ids:
        if run_id and "/" not in run_id and ".." not in run_id:
            (TRACES_DIR / f"{run_id}.jsonl").unlink(missing_ok=True)
    return None
