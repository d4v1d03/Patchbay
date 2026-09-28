"""Server-Sent Events: replay from SQLite, then tail the Redis stream."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from patchbay.db.engine import db_session
from patchbay.db.models import TERMINAL, Session
from patchbay.events import EV_STATUS
from patchbay.events.bus import get_bus

router = APIRouter(prefix="/api")

KEEPALIVE_MS = 15_000


def _frame(ev: dict) -> dict:
    return {"id": str(ev["id"]), "event": ev["type"], "data": json.dumps(ev)}


def _is_terminal(ev: dict) -> bool:
    return ev["type"] == EV_STATUS and ev["payload"].get("status") in TERMINAL


async def event_stream(request: Request, session_id: str, last_id: int):
    bus = get_bus()
    for ev in await run_in_threadpool(bus.replay, session_id, last_id):
        last_id = ev["id"]
        yield _frame(ev)
        if _is_terminal(ev):
            return
    while not await request.is_disconnected():
        events = await run_in_threadpool(bus.poll, session_id, last_id, KEEPALIVE_MS)
        if not events:
            yield {"comment": "keepalive"}
            continue
        for ev in events:
            last_id = ev["id"]
            yield _frame(ev)
            if _is_terminal(ev):
                return


@router.get("/sessions/{session_id}/events")
async def session_events(request: Request, session_id: str, after: int = 0):
    with db_session() as db:
        if db.get(Session, session_id) is None:
            raise HTTPException(404, "no such session")
    header = request.headers.get("last-event-id")
    last_id = int(header) if header and header.isdigit() else after
    return EventSourceResponse(
        event_stream(request, session_id, last_id), ping=KEEPALIVE_MS // 1000
    )
