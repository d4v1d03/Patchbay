from patchbay.db.engine import db_session
from patchbay.db.models import Session
from patchbay.events import EV_STATUS, EV_TOOL_CALL


def _session():
    with db_session() as db:
        s = Session(title="t", prompt="p")
        db.add(s)
        db.flush()
        return s.id


def test_publish_is_durable_and_replayable(bus):
    sid = _session()
    e1 = bus.publish(sid, EV_STATUS, {"status": "running"})
    e2 = bus.publish(
        sid, EV_TOOL_CALL, {"call_id": "c1", "name": "bash", "arguments": {"command": "ls"}}
    )
    assert e2["id"] > e1["id"]
    assert [e["type"] for e in bus.replay(sid)] == [EV_STATUS, EV_TOOL_CALL]
    assert [e["id"] for e in bus.replay(sid, after_id=e1["id"])] == [e2["id"]]
    assert bus.replay("nope") == []


def test_tail_resumes_after_id_without_gap_or_duplicate(bus):
    sid = _session()
    ids = [bus.publish(sid, EV_STATUS, {"status": f"s{i}"})["id"] for i in range(3)]
    gen = bus.tail(sid, after_id=ids[0], block_ms=1)
    got = [next(gen), next(gen)]
    assert [g["id"] for g in got] == ids[1:]
    assert got[0]["payload"] == {"status": "s1"} and got[0]["session_id"] == sid
    assert next(gen) is None  # nothing new → keepalive tick
    new = bus.publish(sid, EV_STATUS, {"status": "s3"})
    assert next(gen)["id"] == new["id"]


def test_cancel_flag_and_lock(bus):
    sid = _session()
    assert not bus.is_cancelled(sid)
    bus.request_cancel(sid)
    assert bus.is_cancelled(sid)
    bus.clear_cancel(sid)
    assert not bus.is_cancelled(sid)
    assert bus.acquire_lock(sid, "w1")
    assert not bus.acquire_lock(sid, "w2")  # held
    bus.release_lock(sid, "w2")  # not the owner → no-op
    assert not bus.acquire_lock(sid, "w2")
    bus.release_lock(sid, "w1")
    assert bus.acquire_lock(sid, "w2")
