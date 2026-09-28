"""One agent run for a stored session. Safe under redelivery: a second copy of
the task arriving while the first holds the lease returns "locked".
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patchbay.agent import context as agent_context
from patchbay.agent import verifier as agent_verifier
from patchbay.agent.loop import Limits, repair_transcript, run_agent
from patchbay.agent.prompts import build_initial_messages, workspace_tree
from patchbay.agent.tools import default_registry
from patchbay.config import get_settings
from patchbay.db.engine import db_session
from patchbay.db.models import Message, Session
from patchbay.events import EV_DIFF, EV_ERROR, EV_SANDBOX_READY, EV_STATUS, EV_TOOL_RESULT, EV_USAGE
from patchbay.events.bus import EventBus, get_bus
from patchbay.llm.client import LLMClient, make_llm
from patchbay.llm.trace import TraceWriter, new_run_id
from patchbay.llm.types import Usage
from patchbay.sandbox import Sandbox, SandboxNotFound, SandboxProtocol

log = logging.getLogger(__name__)

SandboxFactory = Callable[[str, str | None, str | None], SandboxProtocol]
PROJECTS_DIR = Path("data/projects")
MAX_PROJECT_BYTES = 50_000_000
MUTATING_TOOLS = {"write_file", "edit_file", "bash"}


def _default_sandbox_factory(
    session_id: str, repo_url: str | None, container_id: str | None
) -> SandboxProtocol:
    if container_id:
        try:
            return Sandbox.attach(container_id)
        except SandboxNotFound:
            log.info("session %s: container %s gone, recreating", session_id, container_id[:12])
    return Sandbox.create(session_id, repo_url=repo_url)


def run_session(
    session_id: str,
    *,
    llm: LLMClient | None = None,
    bus: EventBus | None = None,
    sandbox_factory: SandboxFactory = _default_sandbox_factory,
    limits: Limits | None = None,
) -> str:
    bus = bus or get_bus()
    settings = get_settings()
    owner = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
    if not bus.acquire_lock(session_id, owner):
        log.warning("session %s already running elsewhere; skipping", session_id)
        return "locked"

    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_renew_lease, args=(bus, session_id, owner, stop_heartbeat), daemon=True
    )
    heartbeat.start()

    try:
        with db_session() as db:
            sess = db.get(Session, session_id)
            if sess is None:
                return "missing"
            if sess.status not in ("queued", "running"):
                # a stale redelivery: every enqueue path sets `queued` first
                log.info("session %s is %s; ignoring stale delivery", session_id, sess.status)
                return "stale"
            cancelled_before_start = bus.is_cancelled(session_id)
            if cancelled_before_start:
                sess.status = "cancelled"
            else:
                sess.status = "running"
        if cancelled_before_start:
            bus.clear_cancel(session_id)
            bus.publish(session_id, EV_STATUS, {"status": "cancelled"})
            return "cancelled"
        with db_session() as db:
            sess = db.get(Session, session_id)
            sess.model = llm.model if llm else settings.llm_model
            prompt, repo_url, container_id = sess.prompt, sess.repo_url, sess.container_id
            prompt_version = sess.prompt_version
            tokens_so_far = Usage(
                sess.prompt_tokens, sess.completion_tokens, sess.cached_tokens
            ).budget_tokens
            transcript: list[dict[str, Any]] = [m.payload for m in sess.messages]
        bus.publish(session_id, EV_STATUS, {"status": "running"})

        sandbox = sandbox_factory(session_id, repo_url, container_id)
        reused = bool(container_id) and getattr(sandbox, "container_id", None) == container_id
        with db_session() as db:
            db.get(Session, session_id).container_id = getattr(sandbox, "container_id", None)
        bus.publish(
            session_id,
            EV_SANDBOX_READY,
            {
                "container_id": getattr(sandbox, "container_id", None),
                "repo_url": repo_url,
                "reused": reused,
            },
        )

        if not transcript:
            transcript = build_initial_messages(
                task=prompt,
                tree=workspace_tree(sandbox),
                repo_url=repo_url,
                version=prompt_version,
            )
        already = _count_messages(session_id)
        if repaired := repair_transcript(transcript):
            log.warning(
                "session %s: answered %d tool call(s) left by a crash", session_id, len(repaired)
            )
        saved = _persist_messages(session_id, transcript, already_saved=already)

        run_id = new_run_id()
        if llm is None:
            llm = make_llm(trace=TraceWriter(run_id))
        with db_session() as db:
            db.get(Session, session_id).last_run_id = run_id

        def emit(kind: str, payload: dict[str, Any]) -> None:
            nonlocal saved
            if kind == EV_USAGE:
                payload = {**payload, "run_id": run_id}
                _add_usage(session_id, payload)  # per turn, so a crash can't lose spend
            bus.publish(session_id, kind, payload)
            saved = _persist_messages(session_id, transcript, already_saved=saved)
            if kind == EV_TOOL_RESULT and payload.get("name") in MUTATING_TOOLS:
                try:
                    bus.publish(session_id, EV_DIFF, sandbox.diff_stat())
                except Exception:  # noqa: BLE001 — stats are cosmetic
                    log.debug("diff_stat failed", exc_info=True)

        outcome = run_agent(
            llm=llm,
            sandbox=sandbox,
            transcript=transcript,
            tools=default_registry(),
            emit=emit,
            should_cancel=lambda: bus.is_cancelled(session_id),
            limits=limits or Limits(tokens_used=tokens_so_far),
            context=agent_context.from_settings(),
            verifier=agent_verifier.from_settings(llm),
        )
        _persist_messages(session_id, transcript, already_saved=saved)
        patch, stat = _capture_patch(sandbox)
        _save_project(session_id, sandbox)

        with db_session() as db:
            sess = db.get(Session, session_id)
            sess.status = outcome.status
            sess.patch = patch
            sess.diff_files, sess.diff_insertions, sess.diff_deletions = (
                stat["files"],
                stat["insertions"],
                stat["deletions"],
            )
            sess.summary = outcome.summary or outcome.question or sess.summary
            sess.error = outcome.error
            sess.finished_at = datetime.now(UTC).replace(tzinfo=None)
        bus.clear_cancel(session_id)
        bus.publish(session_id, EV_STATUS, {"status": outcome.status, "error": outcome.error})
        return outcome.status

    except Exception as e:  # noqa: BLE001 — record, don't let Celery retry forever
        log.exception("session %s crashed", session_id)
        msg = f"{type(e).__name__}: {e}"
        with db_session() as db:
            sess = db.get(Session, session_id)
            if sess is not None:
                sess.status = "failed"
                sess.error = msg
                sess.finished_at = datetime.now(UTC).replace(tzinfo=None)
        bus.publish(session_id, EV_ERROR, {"message": msg, "recoverable": False})
        bus.publish(session_id, EV_STATUS, {"status": "failed", "error": msg})
        return "failed"
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=2)
        bus.release_lock(session_id, owner)


def _add_usage(session_id: str, u: dict[str, Any]) -> None:
    with db_session() as db:
        sess = db.get(Session, session_id)
        if u.get("role") != "verifier":  # a review is not an agent step
            sess.steps += 1
        sess.prompt_tokens += u["prompt_tokens"]
        sess.completion_tokens += u["completion_tokens"]
        sess.cached_tokens += u["cached_tokens"]


def _renew_lease(bus: EventBus, session_id: str, owner: str, stop: threading.Event) -> None:
    """Keep the lease alive while this run is. When the process dies the lease
    stops being renewed and expires, which is how an orphan is detected."""
    interval = max(5, get_settings().lease_ttl_s // 3)
    while not stop.wait(interval):
        if not bus.renew_lock(session_id, owner):
            log.warning("session %s: lost the lease while running", session_id)
            return


def _capture_patch(sandbox: SandboxProtocol) -> tuple[str | None, dict[str, int]]:
    try:
        return sandbox.diff() or None, sandbox.diff_stat()
    except Exception:  # noqa: BLE001 — never fail a finished run over a diff
        log.warning("could not capture patch", exc_info=True)
        return None, {"files": 0, "insertions": 0, "deletions": 0}


def _save_project(session_id: str, sandbox: SandboxProtocol) -> None:
    try:
        data = sandbox.export_zip(folder=session_id)
    except Exception:  # noqa: BLE001 — the run's result doesn't depend on it
        log.warning("could not export the project", exc_info=True)
        return
    if len(data) > MAX_PROJECT_BYTES:
        log.warning("session %s: project zip is %d bytes, not saved", session_id, len(data))
        return
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROJECTS_DIR / f"{session_id}.zip.tmp"
    tmp.write_bytes(data)
    tmp.replace(project_zip_path(session_id))  # atomic: never a half-written download


def project_zip_path(session_id: str) -> Path:
    return PROJECTS_DIR / f"{session_id}.zip"


def _count_messages(session_id: str) -> int:
    with db_session() as db:
        return db.query(Message).filter(Message.session_id == session_id).count()


def _persist_messages(
    session_id: str, transcript: list[dict[str, Any]], *, already_saved: int
) -> int:
    if len(transcript) <= already_saved:
        return already_saved
    with db_session() as db:
        for seq in range(already_saved, len(transcript)):
            m = transcript[seq]
            db.add(Message(session_id=session_id, seq=seq, role=m["role"], payload=m))
    return len(transcript)
