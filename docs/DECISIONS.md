# Decisions

Architecture decision records: what was chosen, the alternatives, and the
consequences. Newest at the bottom.

---

## ADR-001 · FastAPI + Celery + Redis

**Context.** The agent loop is long-running (minutes), does blocking I/O
(LLM calls, `docker exec`), and must be cancellable; HTTP handlers must stay
responsive.

**Decision.** FastAPI for HTTP; Celery workers on a Redis broker for runs;
Redis also carries the live event stream and cancel flags.

**Alternatives.** (a) `asyncio.create_task` inside the web process — simplest,
but a web restart kills every run and there's no isolation between HTTP and
agent work. (b) `arq`/`dramatiq`/`rq` — lighter than Celery, but Celery
has beat scheduling built in and is the more common choice. (c) A
dedicated agent service with its own queue — overkill at this size.

**Consequences.** Two processes to run in dev. Worker code is synchronous
(Celery tasks are sync), so the `docker` and `openai` SDKs are used in their
sync forms — which is actually simpler. Clear seam: `web` never imports
`sandbox/` or `llm/`.

---

## ADR-002 · Server-rendered HTML + vanilla JS, no SPA

**Context.** UI needs: a form, a list, and one live-updating timeline.

**Decision.** Jinja2 templates, one CSS file, one `session.js` using
`EventSource`. No bundler, no framework.

**Alternatives.** React/Vite — nicer component model but adds a toolchain and
a second language surface; HTMX — attractive, but SSE-driven DOM patching of
a timeline is easier to reason about in ~200 lines of plain JS.

**Consequences.** Zero build step; the whole UI is readable in one sitting.
If the timeline grows complex (streaming deltas, xterm), revisit.

---

## ADR-003 · One OpenAI-compatible client for all providers

**Context.** Primary targets are Chinese models (DeepSeek, Qwen, Kimi, GLM,
MiniMax). Every one of them exposes an OpenAI-compatible chat endpoint with
`tools`/`tool_calls`; OpenRouter fronts all of them with one key.

**Decision.** Use the official `openai` Python SDK with `base_url`, `api_key`
and `model` from config. Wrap it in a small `LLMClient` that normalises the
response into `AssistantTurn` and records usage.

**Alternatives.** Per-provider SDKs (DashScope SDK, etc.) — more features,
more surface, no benefit for chat+tools. LiteLLM — does the same
normalisation, but adds a layer between the loop and the API for no gain here.

**Consequences.** Switching models is an `.env` edit. Provider quirks
(`reasoning_content`, differing `finish_reason`s, rate-limit headers) are
handled in one file and documented in Field notes.

---

## ADR-004 · Non-streaming LLM calls

**Context.** Streaming tool-call arguments over SSE from OpenAI-compatible
providers is fiddly (partial JSON, provider differences) and adds little:
what users want to watch is *commands and their output*, not tokens.

**Decision.** `chat()` is a single blocking request. Liveness comes from
tool-call/tool-result events.

**Alternatives.** Stream from day one — better final-message UX, higher
complexity in the loop itself, for little user-visible gain.

**Consequences.** A visible pause of 5–30 s while the model thinks; the UI
shows a "thinking…" state. Streaming assistant text is a later addition.

---

## ADR-005 · Server-Sent Events, not WebSockets

**Context.** Live updates flow strictly server → browser. The only
client → server messages (new task, follow-up, stop) are ordinary POSTs.

**Decision.** SSE via `sse-starlette`, with `id:` on every frame so the
browser's automatic reconnect sends `Last-Event-ID`.

**Alternatives.** WebSockets — bidirectional, but that's unneeded, and you
give up free reconnection/replay semantics and plain-HTTP debuggability
(`curl -N`).

**Consequences.** Proxies need keepalives (sent every 15 s). One SSE
connection per open session tab — fine for a single-user tool.

---

## ADR-006 · Redis Streams + SQLite for events (not pub/sub alone)

**Context.** The SSE endpoint must (a) replay history on connect/reconnect
and (b) tail new events with no gap between the two. Redis pub/sub is
fire-and-forget: anything published between "read history" and "subscribe"
is lost.

**Decision.** `publish()` writes the event to SQLite (durable, the source of
truth) then `XADD`s it to `session:{id}:events` (`MAXLEN ~1000`). The SSE
handler replays from SQLite `> Last-Event-ID`, then `XREAD BLOCK`s from the
same id. Stream entries carry the SQLite `event.id` so the two are aligned.

**Alternatives.** Pub/sub + DB replay — the race above. Poll SQLite every
500 ms — works, but wasteful. Kafka/NATS.

**Consequences.** Two writes per event (cheap). If Redis loses the stream
(restart, trim), replay from SQLite still works.

---

## ADR-007 · Separate `messages` (transcript) and `events` (timeline)

**Context.** Tempting to store one list and derive both.

**Decision.** Two tables. `messages` is exactly what the LLM API consumes.
`events` is what the UI consumes, with metadata the model never sees.

**Alternatives.** One table with a `kind` column — every consumer then
filters and maps, and the transcript shape (tool-call ids, roles) gets
polluted by UI concerns.

**Consequences.** A tool call is written twice (as an assistant message
and as an event) — deliberate. Follow-ups resume from `messages`; page
loads render from `events`.

---

## ADR-008 · Sibling containers via the Docker socket; `sleep infinity` + `exec`

**Context.** The worker must create/destroy sandboxes. In compose the worker
is itself a container.

**Decision.** Mount `/var/run/docker.sock` into the worker; sandboxes are
*sibling* containers on the host daemon (not docker-in-docker). Each
sandbox starts `sleep infinity` as PID 1; every tool call is a
`docker exec`.

**Alternatives.** Docker-in-Docker (privileged, slow, nested daemon).
One container per command (`docker run` each time) — loses state between
calls and pays startup cost 40× per run. SSH into a long-lived VM — heavier
to provision, but the right answer at scale.

**Consequences.** The worker is root-equivalent on the host — documented,
acceptable for a local dev tool, and the reason `Sandbox` is an interface
you could back with a remote daemon or gVisor later.

---

## ADR-009 · Explicit file tools alongside `bash`

**Context.** A model with only `bash` can do everything, in theory.

**Decision.** Provide `read_file`, `write_file`, `edit_file` (unique-match
string replace), `list_files`, `search` in addition to `bash`.

**Alternatives.** `bash` only — fewer tools to write, but models make many
more quoting/heredoc mistakes, outputs are unstructured, and the UI can't
show "edited `app.py`". Full patch-apply tool (`apply_patch`) — powerful,
but models produce malformed hunks often enough to frustrate.

**Consequences.** ~150 more lines of tool code; markedly higher success
rate and a far better trace. `edit_file` errors are written for the model
("`old` found 3 times; include more context") — that phrasing matters.

---

## ADR-010 · `finish` and `ask_user` are tools

**Context.** How does a run end? "The model stopped emitting tool calls" is
ambiguous — it may be summarising mid-task.

**Decision.** Two terminal tools. `finish(summary)` ends the run as
`completed`; `ask_user(question)` ends it as `waiting_for_user`. A turn with
text and no tool calls is treated as `completed` too (models sometimes
forget), but the prompt asks for `finish`.

**Alternatives.** Parse the assistant text for "DONE" markers — brittle.

**Consequences.** The state machine is explicit and testable; the UI can
render the final summary distinctly.

---

## ADR-011 · SQLite, not Postgres

**Context.** Single user, one machine, three small tables.

**Decision.** SQLite file under `data/`, via SQLAlchemy so the swap is a URL
change.

**Alternatives.** Postgres in compose — one more container and migrations
for no gain at this size.

**Consequences.** `web` and `worker` share the file (fine with WAL mode).
Multi-writer contention is not a concern at one run at a time.

---

## ADR-012 · Timeouts via the container's `timeout`, not from outside

**Context.** `exec_run` blocks with no timeout parameter; cancelling an exec
from the API is awkward and leaves zombie processes.

**Decision.** Wrap every command: `timeout -k 5 <n>s bash -lc '<cmd>'`.
Exit code 124 → `timed_out=True`.

**Alternatives.** Run `exec_run` in a thread and abandon it on timeout —
leaks the process inside the container. Kill the container — loses state.

**Consequences.** Timeouts are reliable and cheap. A `Stop` during a long
command waits for that command's timeout (max 600 s); acceptable for now,
noted in ARCHITECTURE §10.

---

## ADR-013 · Keep sandboxes alive after a run; reap by TTL

**Context.** Follow-ups should be instant and keep the agent's changes.

**Decision.** Container stays up after `completed`/`waiting_for_user`; a
beat task removes containers older than `SANDBOX_TTL_MINUTES`. A follow-up
on a reaped session recreates and re-clones, with a warning event.

**Alternatives.** Destroy on finish (loses work, slow follow-ups);
keep forever (disk/CPU leak).

**Consequences.** Users can lose unsaved changes after the TTL — the diff
panel makes "download the patch" obvious. Re-applying the last patch
automatically is a later addition.

---

## ADR-014 · `uv` for dependency management

**Context.** Need reproducible installs and a lockfile; Python 3.14 on the
dev machine may lack wheels for some packages.

**Decision.** `uv` with `pyproject.toml` + `uv.lock`, `requires-python =
">=3.12"`. `uv` can fetch a managed 3.12/3.13 if 3.14 causes trouble.

**Alternatives.** `venv` + `requirements.txt` (like fleetwatch) — works,
no lockfile; Poetry — slower, heavier.

**Consequences.** One-line install for `uv`; `uv run` everywhere in the
docs. `pip install -e .` still works for anyone without `uv`.

---

---

## ADR-015 · The patch is stored on the session when a run ends

**Context.** The workspace lives in a container that is reaped after a TTL;
the result of a session must not disappear with it.

**Decision.** At the end of every run the worker computes `git diff` and
`--numstat` and stores both on the session row. The diff endpoint serves the
live diff while the container exists and the stored one afterwards, and says
which. A `.patch` download is the way results leave the system.

**Alternatives.** Push a branch from the sandbox — needs credentials inside
the container, which the design forbids. Keep containers forever — a leak.

**Consequences.** One more `git diff` per run (milliseconds). Results are
durable; workspaces stay disposable.

---

## ADR-016 · A scripted demo model behind the same interface

**Context.** Developing the UI, running the compose stack, and load-testing
should not require an API key or spend tokens.

**Decision.** `LLM_BASE_URL=fake` selects `DemoLLM`, which replays a short
plausible script (orient, write, test, finish; ask on request; answer
follow-ups) through the real tool registry against a real sandbox, and
writes traces like the real client.

**Alternatives.** Mock at the HTTP layer — brittle across providers; skip
and always use a real key — slow, costly, rate-limited.

**Consequences.** The whole stack runs end to end in seconds with no
credentials, and tests of the runner and UI have a deterministic model.

---

## ADR-017 · Provider quirks are absorbed in one place

**Context.** OpenAI-compatible providers differ in details: where cached
token counts live, `reasoning_content` on thinking models, Gemini's
`thought_signature` that must be echoed back on every tool call, per-minute
and per-day quota errors.

**Decision.** `parse_response` normalises every response into
`AssistantTurn`; unknown fields on tool calls are kept on `ToolCall.extra`
and re-emitted by `to_message()`; rate-limit handling lives in the client.
Nothing outside `llm/` knows which provider is in use.

**Consequences.** Adding a provider is a change to one file. Transcripts
stored in the database carry the extras, so follow-ups remain valid across
providers that require them.

---

## ADR-018 · Short leases with a heartbeat; repair the transcript on resume

**Context.** Killing a worker mid-run showed three problems. The run lease was
4 hours, so the sweeper left the orphaned session alone for 4 hours. A crash
between saving an assistant's tool call and saving its result left an
unanswered call in the stored transcript, which OpenAI-compatible providers
reject. Usage was added to the session only at the end of a run, so a crash
lost it — including from the token budget.

**Decision.** Leases are 90 s and renewed every 30 s by a thread that lives as
long as the run; a dead worker's lease expires on its own. On resume,
unanswered tool calls at the tail of the transcript get an explicit
"interrupted — check before retrying" result. Usage and step counts are added
to the session per turn. Runs ignore deliveries for sessions not in `queued` or
`running`. Containers have `restart: unless-stopped`.

**Alternatives.** Rely on Celery redelivery alone — the visibility timeout has
to exceed the longest run, so recovery would take hours. Drop the unanswered
call instead of answering it — hides from the model that a side effect may
already have happened.

**Consequences.** A dead worker's session resumes within about
`LEASE_TTL_S + MAINTENANCE_INTERVAL_S`, continues rather than restarts, and
spend is never lost. One Redis `EXPIRE` every 30 s per active run.

---

## ADR-019 · The agent package stays free of infrastructure

**Context.** The session runner and the maintenance jobs lived in `agent/`
next to the loop, and the event constants were re-exported from a package
whose `__init__` imported the Redis-backed bus — so importing the "pure" loop
loaded SQLAlchemy, Redis and the database layer.

**Decision.** Two packages with one direction of dependency. `agent/` holds
the loop, tools, prompts and the standalone CLI and imports nothing from
`db`, `events.bus`, `jobs` or Celery. `jobs/` holds the runner, maintenance
and the enqueue helper, and may import anything below it. `events/` exports
only the event-name constants; the bus is imported from `events.bus`.
`web` starts runs through `jobs.queue`, so `jobs` never imports `web`.

**Consequences.** The agent can be embedded, tested or evaluated without a
database or broker. A check for this is one line: import `patchbay.agent.loop`
and assert `sqlalchemy` and `redis` are not in `sys.modules`.

---

## ADR-020 · Static system prompt; session details in the first user message

**Context.** Providers bill a repeated prompt prefix at a fraction of the price
(DeepSeek: 2%). The tool definitions are serialised after the system prompt, so
a system prompt that contains the repository, network setting and file tree
differs per session and stops even the tools from being shared.

**Decision.** From prompt v2 the system prompt is byte-identical for every
session; the per-session block (`session_v2.md`) is prepended to the first user
message. The block is descriptive, not imperative.

**Alternatives.** Keep the layout and accept no cross-session caching; put
session details at the end of the system prompt (measured: 20% cached).

**Consequences.** 88% of a new session's first request is served from the
provider's cache (was 0%). Pass rate indistinguishable from v1 at the sample
sizes measured. See `docs/EVALS.md`.

---

## ADR-021 · Context compaction exists but is off by default

**Context.** Resending the whole conversation every step makes prompts grow.
Observation masking (stubbing old tool outputs) shrinks them, but rewrites the
middle of the prompt, which breaks the provider's prefix cache, and hides
content the model may need again.

**Decision.** Implement masking with hysteresis (compact at the budget, down to
half of it; stubs byte-stable) behind `CONTEXT_BUDGET_TOKENS`, default 0.

**Alternatives.** On by default — measured on the long eval task: a 3k budget
raised cost 47% (re-reads of elided files, cache hit 86% → 77%) and a 5k
budget made no measurable difference. LLM summarisation — an extra call per
compaction that still breaks the cache.

**Consequences.** At the context sizes this agent reaches (≤ ~25k tokens
against a 1M window) the cheapest option is to send everything and let the
cache absorb it. Compaction is available for long sessions, small-context
models or providers without cheap caching.

---

## ADR-022 · Model routing and provider fallback live outside the agent

**Context.** Different calls could go to different models (a cheap one for
reviews, a strong one for the loop), and a provider outage or exhausted quota
currently fails the run.

**Decision.** Patchbay talks to one OpenAI-compatible endpoint
(`LLM_BASE_URL`). Choosing a model per request and failing over between
providers belong in a gateway in front of it, which Patchbay then uses by
pointing `LLM_BASE_URL` at it. Inside Patchbay, every call uses the configured
model, including the reviewer's.

**Alternatives.** A fallback wrapper and per-role model settings in
`llm/client.py`: less to deploy, but routing policy would then live in each
application that calls models, and mid-transcript provider switches need
provider-specific handling (signed tool calls, reasoning fields) that a
gateway handles once.

**Consequences.** No failover today: a provider outage fails the run with a
clear `LLM call failed` error, and the run can be retried by sending a
follow-up.

---

## ADR-023 · A reviewer before `finish`, off by default

**Context.** The agent decides for itself when it is done, and its own tests
are the only check. A second opinion before the run is accepted could catch
work that is wrong while its tests pass.

**Decision.** `agent/verifier.py`: one extra model call (no tools, its own
prompt, JSON mode validated with pydantic) sees the request, the summary, the
diff and the last command's output, and returns `{approve, issues}`. A
rejection goes back to the agent and the run continues; after
`VERIFY_ROUNDS` rejections the next ending is accepted, and a failed review
lets the ending stand. `VERIFY_ROUNDS=0` by default.

**Alternatives.** Let the reviewer run commands (an agent reviewing an
agent): catches behaviour it can't see in a diff, at several times the cost.
A plan step before the loop: this suite is too easy to show whether planning
helps. Review with the hidden tests: they don't exist outside evals.

**Consequences.** Measured (docs/EVALS.md): no wrong rejections of correct
work (42/42 approved), every planted bug the hidden tests confirm rejected
(20/20, 11 visible only in the diff); in the loop +74% cost with nothing to
catch on this suite. Off until a workload with weaker tests shows a benefit.

---

## ADR-024 · Diffs are taken against where the session started

**Context.** The diff (shown in the UI, saved as the session's patch, and
given to the reviewer) was `git diff` against the index, so anything the
agent committed disappeared from it.

**Decision.** At workspace creation, `refs/patchbay/base` points at the
cloned `HEAD`, or at git's empty tree for an empty workspace; diffs are taken
against it. A ref rather than a tag or commit, so it doesn't show in the
agent's `git log`.

**Alternatives.** Forbid commits in the prompt (the agent might commit
anyway); snapshot the files at start (duplicates what git already does).

**Consequences.** The patch is everything since the session started,
committed or not. Sandboxes created before the change have no ref and fall
back to `HEAD`.

The first version staged new files in the agent's own index (`git add -N`).
A test run showed two problems: the agent's `git status` listed every file as
added, and a build directory seen before the agent wrote `.gitignore` stayed
in the patch for good. Diffs now use a private index, reset to the base on
every call.

---

## ADR-025 · The project download is a zip file on the data volume

**Context.** The patch is the right format for applying changes to a repo you
already have, but for a project built from nothing it's the wrong thing to
hand over: a text diff, mostly lockfile and build output.

**Decision.** When a run ends, the worker exports the workspace with
`git archive` from the same private index the diff uses, so `.gitignore`
applies and `node_modules` and `.git` are left out, and writes it to
`data/projects/<session>.zip` (atomically, via a temp file). `web` serves the
file; it has no Docker access, so it can't build the zip itself.

**Alternatives.** Build the zip on request from the live container: needs
Docker access in `web` and fails once the sandbox is reaped. Store it in
SQLite: a few hundred KB per run in a row that every session query would
otherwise not need.

**Consequences.** The download reflects the end of the last run, not a run in
progress. Zips over 50 MB are skipped with a warning. Deleting a session
deletes its zip.
