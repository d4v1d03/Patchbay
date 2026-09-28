"""Start a session, SIGKILL the worker holding its lease, and report what happens
until the session finishes. Use the demo model so it costs nothing:

    docker compose up -d --scale worker=2      # with DEMO_DELAY_S=6
    uv run python tools/chaos_kill_worker.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time

import httpx
import redis

TERMINAL = {"completed", "failed", "cancelled", "waiting_for_user"}


def containers() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"], capture_output=True, text=True
    ).stdout
    return dict(line.split("\t") for line in out.splitlines() if "\t" in line)


def main() -> int:
    ap = argparse.ArgumentParser(prog="chaos_kill_worker")
    ap.add_argument("--base", default="http://localhost:8088")
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--prompt", default="make hello with tests")
    ap.add_argument("--watch", type=float, default=180.0, help="seconds to watch after the kill")
    ap.add_argument("--no-kill", action="store_true", help="baseline run, no chaos")
    ap.add_argument(
        "--kill-after", type=float, default=0.0, help="seconds to let the run progress first"
    )
    a = ap.parse_args()

    r = redis.from_url(a.redis)
    t0 = time.monotonic()

    def log(msg: str) -> None:
        print(f"[{time.monotonic() - t0:6.1f}s] {msg}", flush=True)

    sid = httpx.post(f"{a.base}/api/sessions", json={"prompt": a.prompt}, timeout=30).json()["id"]
    log(f"created session {sid}")

    owner = None
    for _ in range(60):
        owner = r.get(f"session:{sid}:lock")
        if owner:
            break
        time.sleep(0.5)
    if not owner:
        log("no lease appeared — is a worker running?")
        return 2
    owner = owner.decode()
    ttl = r.ttl(f"session:{sid}:lock")
    log(f"lease held by {owner} (ttl {ttl}s)")

    if not a.no_kill:
        if a.kill_after:
            time.sleep(a.kill_after)
        host = owner.split(":")[0]
        name = next((n for cid, n in containers().items() if cid.startswith(host[:12])), None)
        if name is None:
            log(f"could not map worker host {host!r} to a container")
            return 2
        status = httpx.get(f"{a.base}/api/sessions/{sid}", timeout=10).json()
        log(f"before kill: status={status['status']} steps={status['steps']}")
        subprocess.run(["docker", "kill", "-s", "KILL", name], capture_output=True, check=True)
        log(f"SIGKILLed {name}")

    seen: set[str] = set()
    deadline = time.monotonic() + a.watch
    while time.monotonic() < deadline:
        s = httpx.get(f"{a.base}/api/sessions/{sid}", timeout=10).json()
        lease = r.ttl(f"session:{sid}:lock")
        key = f"{s['status']}/{s['steps']}/{lease > 0}"
        if key not in seen:
            seen.add(key)
            log(f"status={s['status']} steps={s['steps']} lease={'held' if lease > 0 else 'gone'}")
        if s["status"] in TERMINAL:
            log(f"terminal: {s['status']} after {s['steps']} steps total")
            print(json.dumps({"session": sid, "status": s["status"], "steps": s["steps"]}))
            return 0 if s["status"] == "completed" else 1
        time.sleep(2)
    log(f"still {s['status']} after {a.watch:.0f}s — not recovered")
    print(json.dumps({"session": sid, "status": s["status"], "steps": s["steps"]}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
