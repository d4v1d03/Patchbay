# Operations

Notes on running Patchbay with more than one process, and what was verified.

## Scaling the web tier

`web` holds no session state — the timeline lives in SQLite and Redis, the run
lease lives in Redis — so replicas are interchangeable:

```bash
docker compose up -d --scale web=3
```

`web` publishes no host port; Traefik discovers the replicas by label and
round-robins between them. `/health` returns the replica's hostname as
`instance`, and every response carries `X-Patchbay-Instance`, so it is possible
to tell which replica served a request.

Verified with three replicas:

| Check | Result |
| --- | --- |
| 30 requests through Traefik | 11 / 10 / 9 across the three replicas |
| SSE stream, its replica killed mid-run | client reconnected to a different replica; events 25–48 received, no gaps, no duplicates; the run finished normally |
| Same kill, client not sending `Last-Event-ID` | no gaps either — it replayed from SQLite — but 4 duplicated events |
| Redis stopped | `/health` → 503 on every replica; Traefik takes them out of rotation within one health-check interval |
| Redis restarted | back in rotation, `/health` → 200, within ~15 s |

Two consequences worth knowing:

* **Resume is what makes a reconnect exact.** `EventSource` sends
  `Last-Event-ID` automatically, and the endpoint replays from that id. A client
  that reconnects without it still loses nothing, because the event log is
  durable, but it will see the earlier events again — so a renderer should be
  idempotent if it may reconnect from scratch.
* **The health check is the contract with the load balancer.** `/health` returns
  503 when Redis or the database is unreachable, which is what lets Traefik stop
  sending traffic to a replica that cannot serve it, instead of returning errors
  to users.

`tools/sse_client.py` is the client used for these checks. It reconnects the way
a browser does and reports every event id it received:

```bash
uv run python tools/sse_client.py http://localhost:8088/api/sessions/<id>/events -v
uv run python tools/sse_client.py <url> --no-resume    # reconnect without Last-Event-ID
```

It exits non-zero if the stream had a gap or a duplicate, so it works in a test.

## Workers

Agent runs go to the `runs` queue, periodic maintenance to `maintenance`, so a
long run never delays the reaper. Workers scale the same way as `web`:

```bash
docker compose up -d --scale worker=2
```

**Leases.** A run takes `SET session:<id>:lock <owner> NX EX 90` and a
background thread renews it every 30 s. A task delivered twice executes once;
a worker that dies stops renewing, so its lease expires within
`LEASE_TTL_S`. The maintenance sweeper re-queues any session still marked
`running` with no lease, and another worker resumes it.

**Resume, not restart.** The transcript is written after every turn and usage
is added to the session per turn, so a resumed run continues from the last
saved message and a crash never loses spend (which would otherwise let a
session slip past `MAX_SESSION_TOKENS`). If the crash landed between an
assistant's tool call and its result, the call is answered on resume with an
"interrupted — check the workspace before retrying" result: providers reject a
transcript with an unanswered call, and the model should not assume the tool
either ran or didn't.

**Stale redeliveries.** With `task_acks_late`, Celery redelivers an unacked
message after its visibility timeout. Every enqueue path sets the session to
`queued` first, so a run that finds its session in any other terminal state
treats the message as stale and exits.

Verified with `tools/chaos_kill_worker.py` against a compose stack on the demo
model (`DEMO_DELAY_S=6`, two workers):

| Scenario | Result |
| --- | --- |
| Worker SIGKILLed two steps into a five-step run | lease expired ~75 s later; sweeper re-queued at the next beat; the other worker resumed at step 3; **completed in 5 LLM calls total**, none repeated; ~145 s from kill to resumption |
| Same, before the fixes | session stuck in `running` — the lease was 4 h, so the sweeper skipped it |
| Crash between a tool call and its result | transcript saved with an unanswered call; on resume it is answered as interrupted (previously a real provider would have rejected every request) |
| All workers dead | beat keeps enqueueing maintenance, nothing executes it; when a worker returns it drains the backlog and rescues the session |
| Worker process exits on its own | `restart: unless-stopped` brings it back in ~6 s |

Worst-case time to resume after a worker dies is roughly
`LEASE_TTL_S + MAINTENANCE_INTERVAL_S + 20 s`. A shorter lease resumes sooner
but risks a live run losing its lease during a long pause (a suspended laptop,
a stalled host), so it is a trade-off, not a free knob.

`docker kill` counts as a manual stop and is **not** restarted by the restart
policy; a crash, OOM kill or process exit is.

```bash
uv run python tools/chaos_kill_worker.py --kill-after 15
```

## Sandboxes

One container per session, labelled `patchbay.session`. They outlive a run so a
follow-up is instant, and are removed by the reaper after
`SANDBOX_TTL_MINUTES`. To clear them by hand:

```bash
make clean-sandboxes
```
