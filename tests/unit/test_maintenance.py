from datetime import datetime, timedelta

from patchbay.db.engine import db_session
from patchbay.db.models import Session
from patchbay.jobs.maintenance import reap, sweep_stuck_sessions


def _session(status="running", container_id="cid", updated_ago=timedelta(minutes=10)):
    with db_session() as db:
        s = Session(title="t", prompt="p", status=status, container_id=container_id)
        db.add(s)
        db.flush()
        s.updated_at = datetime.utcnow() - updated_ago
        return s.id


def test_reap_clears_container_ids_of_reaped_sessions(bus):
    a, b = _session(), _session()
    reaped = reap(ttl_minutes=60, reaper=lambda ttl: [a, "", "s_unknown"])
    assert reaped == [a, "s_unknown"]
    with db_session() as db:
        assert db.get(Session, a).container_id is None
        assert db.get(Session, b).container_id == "cid"


def test_sweep_requeues_orphans_only(bus):
    orphan = _session()  # running, no lock, updated 10 min ago
    held = _session()
    bus.acquire_lock(held, "worker-1")
    fresh = _session(updated_ago=timedelta(seconds=10))  # running, no lock, but just updated
    done = _session(status="completed")
    queued = []
    rescued = sweep_stuck_sessions(bus=bus, enqueue=queued.append)
    assert rescued == [orphan] and queued == [orphan]
    with db_session() as db:
        assert db.get(Session, orphan).status == "queued"
        assert db.get(Session, held).status == "running"
        assert db.get(Session, fresh).status == "running"
        assert db.get(Session, done).status == "completed"
    assert bus.replay(orphan)[-1]["payload"]["status"] == "queued"
