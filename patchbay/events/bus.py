"""Publish/replay/tail session events.

publish() writes the event to SQLite (durable) and XADDs it to the session's
Redis stream (live). The stream entry id is `<sqlite id>-0`, so a client that
last saw event N resumes with XREAD from `N-0` — no gap, no duplicates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import redis

from patchbay.config import get_settings
from patchbay.db.engine import db_session
from patchbay.db.models import Event

log = logging.getLogger(__name__)

STREAM_MAXLEN = 2000
STREAM_TTL_S = 7 * 24 * 3600


def stream_key(session_id: str) -> str:
    return f"session:{session_id}:events"


def cancel_key(session_id: str) -> str:
    return f"session:{session_id}:cancel"


def lock_key(session_id: str) -> str:
    return f"session:{session_id}:lock"


class EventBus:
    def __init__(self, r: redis.Redis):
        self.redis = r

    def publish(self, session_id: str, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        with db_session() as db:
            ev = Event(session_id=session_id, type=type_, payload=payload)
            db.add(ev)
            db.flush()
            data = ev.to_dict()
        key = stream_key(session_id)
        try:
            self.redis.xadd(
                key,
                {"type": type_, "payload": json.dumps(payload), "ts": data["ts"]},
                id=f"{data['id']}-0",
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            self.redis.expire(key, STREAM_TTL_S)
        except redis.ResponseError as e:
            # out-of-order id from a concurrent publisher; SQLite still has it
            log.warning("xadd %s: %s", key, e)
        return data

    def replay(self, session_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with db_session() as db:
            rows = (
                db.query(Event)
                .filter(Event.session_id == session_id, Event.id > after_id)
                .order_by(Event.id)
                .all()
            )
            return [r.to_dict() for r in rows]

    def tail(
        self, session_id: str, after_id: int = 0, block_ms: int = 15_000
    ) -> Iterator[dict[str, Any] | None]:
        """Yield events after `after_id` as they arrive; yields None when a
        block period passes with nothing (callers send a keepalive)."""
        last = f"{after_id}-0" if after_id else "0-0"
        key = stream_key(session_id)
        while True:
            res = self.redis.xread({key: last}, count=100, block=block_ms)
            if not res:
                yield None
                continue
            for _key, entries in res:
                for entry_id, fields in entries:
                    last = entry_id
                    yield _decode(session_id, entry_id, fields)

    def poll(
        self, session_id: str, after_id: int = 0, block_ms: int = 15_000
    ) -> list[dict[str, Any]]:
        last = f"{after_id}-0" if after_id else "0-0"
        res = self.redis.xread({stream_key(session_id): last}, count=200, block=block_ms)
        out: list[dict[str, Any]] = []
        for _key, entries in res or []:
            for entry_id, fields in entries:
                out.append(_decode(session_id, entry_id, fields))
        return out

    def request_cancel(self, session_id: str) -> None:
        self.redis.set(cancel_key(session_id), "1", ex=3600)

    def is_cancelled(self, session_id: str) -> bool:
        return bool(self.redis.exists(cancel_key(session_id)))

    def clear_cancel(self, session_id: str) -> None:
        self.redis.delete(cancel_key(session_id))

    def acquire_lock(self, session_id: str, owner: str, ttl_s: int | None = None) -> bool:
        ttl = ttl_s or get_settings().lease_ttl_s
        return bool(self.redis.set(lock_key(session_id), owner, nx=True, ex=ttl))

    def renew_lock(self, session_id: str, owner: str, ttl_s: int | None = None) -> bool:
        key = lock_key(session_id)
        if (self.redis.get(key) or b"") not in (owner, owner.encode()):
            return False
        return bool(self.redis.expire(key, ttl_s or get_settings().lease_ttl_s))

    def forget(self, session_id: str) -> None:
        self.redis.delete(stream_key(session_id), cancel_key(session_id), lock_key(session_id))

    def lock_held(self, session_id: str) -> bool:
        return bool(self.redis.exists(lock_key(session_id)))

    def release_lock(self, session_id: str, owner: str) -> None:
        key = lock_key(session_id)
        if (self.redis.get(key) or b"") in (owner, owner.encode()):
            self.redis.delete(key)


def _decode(session_id: str, entry_id: str | bytes, fields: dict) -> dict[str, Any]:
    def s(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else v

    entry_id = s(entry_id)
    f = {s(k): s(v) for k, v in fields.items()}
    return {
        "id": int(entry_id.split("-")[0]),
        "session_id": session_id,
        "type": f["type"],
        "payload": json.loads(f["payload"]),
        "ts": f.get("ts"),
    }


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus(redis.from_url(get_settings().redis_url))
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus
