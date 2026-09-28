from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from patchbay.config import get_settings
from patchbay.db.engine import db_session
from patchbay.db.models import Session
from patchbay.events import EV_STATUS
from patchbay.events.bus import EventBus, get_bus
from patchbay.sandbox import reap_sandboxes

log = logging.getLogger(__name__)

# a run acquires its lease before it is marked running and renews it while alive,
# so "running with no lease" already means orphaned; the grace only covers clock
# skew and the moment between the two writes
ORPHAN_GRACE = timedelta(seconds=20)


def reap(
    *, ttl_minutes: int | None = None, reaper: Callable[[int], list[str]] = reap_sandboxes
) -> list[str]:
    ttl = ttl_minutes if ttl_minutes is not None else get_settings().sandbox_ttl_minutes
    reaped = [sid for sid in reaper(ttl) if sid]
    if reaped:
        with db_session() as db:
            for sess in db.query(Session).filter(Session.id.in_(reaped)):
                sess.container_id = None
    return reaped


def sweep_stuck_sessions(
    *,
    bus: EventBus | None = None,
    enqueue: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Sessions marked `running` whose lease is gone were abandoned by a dead
    worker. Re-queue them so the run resumes from the saved transcript."""
    bus = bus or get_bus()
    if enqueue is None:
        from patchbay.jobs.queue import enqueue_run

        enqueue = enqueue_run
    now = now or datetime.now(UTC).replace(tzinfo=None)
    rescued: list[str] = []
    with db_session() as db:
        running = db.query(Session).filter(Session.status == "running").all()
        for sess in running:
            if bus.lock_held(sess.id):
                continue  # a worker is on it
            if now - sess.updated_at < ORPHAN_GRACE:
                continue  # just started or just finished; give it a moment
            sess.status = "queued"
            rescued.append(sess.id)
    for sid in rescued:
        log.warning("session %s was orphaned by a dead worker; re-queued", sid)
        bus.publish(sid, EV_STATUS, {"status": "queued", "note": "resumed after worker restart"})
        enqueue(sid)
    return rescued
