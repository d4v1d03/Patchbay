"""Resumable SSE client, for checking that a stream survives losing a server.
Reconnects with Last-Event-ID as a browser does, and reports gaps and
duplicates.

    uv run python tools/sse_client.py http://localhost:8088/api/sessions/s_x/events
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

import httpx

TERMINAL = {"completed", "failed", "cancelled", "waiting_for_user"}


@dataclass
class Result:
    ids: list[int] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)
    reconnects: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False

    @property
    def gaps(self) -> list[int]:
        if not self.ids:
            return []
        return sorted(set(range(min(self.ids), max(self.ids) + 1)) - set(self.ids))

    @property
    def duplicates(self) -> list[int]:
        return sorted({i for i in self.ids if self.ids.count(i) > 1})


def stream(
    url: str, *, timeout_s: float = 120.0, verbose: bool = False, resume: bool = True
) -> Result:
    r = Result()
    last_id: int | None = None
    deadline = time.monotonic() + timeout_s
    while not r.finished and time.monotonic() < deadline:
        headers = {"Accept": "text/event-stream"}
        if resume and last_id is not None:
            headers["Last-Event-ID"] = str(last_id)
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, read=30.0)) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    served_by = resp.headers.get("x-patchbay-instance", "?")
                    r.instances.append(served_by)
                    if verbose:
                        print(f"# connected to {served_by}", flush=True)
                    data = None
                    for line in resp.iter_lines():
                        if line.startswith(":"):
                            continue
                        if not line:  # blank line ends a frame
                            if data is not None:
                                payload = json.loads(data)
                                r.ids.append(payload["id"])
                                r.types.append(payload["type"])
                                last_id = payload["id"]
                                if verbose:
                                    print(f"  {payload['id']:>4} {payload['type']}", flush=True)
                                if (
                                    payload["type"] == "session.status"
                                    and payload["payload"].get("status") in TERMINAL
                                ):
                                    r.finished = True
                                    break
                            data = None
                            continue
                        key, _, value = line.partition(":")
                        if key == "data":  # `event:` and `id:` are redundant here:
                            data = value.lstrip()  # the payload carries both
        except Exception as e:  # noqa: BLE001 — a dropped server is the point of this tool
            r.errors.append(f"{type(e).__name__}: {e}")
        if not r.finished:
            r.reconnects += 1
            time.sleep(0.4)  # what a browser's EventSource does before retrying
    return r


def main() -> int:
    ap = argparse.ArgumentParser(prog="sse_client")
    ap.add_argument("url")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", help="print a machine-readable summary")
    ap.add_argument(
        "--no-resume", action="store_true", help="reconnect without Last-Event-ID (shows the gap)"
    )
    a = ap.parse_args()
    r = stream(a.url, timeout_s=a.timeout, verbose=a.verbose, resume=not a.no_resume)
    summary = {
        "received": len(r.ids),
        "first": r.ids[0] if r.ids else None,
        "last": r.ids[-1] if r.ids else None,
        "gaps": r.gaps,
        "duplicates": r.duplicates,
        "instances": r.instances,
        "reconnects": r.reconnects,
        "errors": r.errors,
        "finished": r.finished,
    }
    print(json.dumps(summary, indent=None if a.json else 2))
    return 0 if r.finished and not r.gaps and not r.duplicates else 1


if __name__ == "__main__":
    sys.exit(main())
