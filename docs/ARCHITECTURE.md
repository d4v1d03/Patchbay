# Architecture

Patchbay runs an LLM-driven coding agent inside a throwaway Docker container
per session and streams what it does to a browser. This document describes the
moving parts and how a request flows through them.

Contents

1. [Processes](#1-processes)
2. [Lifecycle of a session](#2-lifecycle-of-a-session)
3. [Data model](#3-data-model)
4. [Events and streaming](#4-events-and-streaming)
5. [Agent loop](#5-agent-loop)
6. [Tools](#6-tools)
7. [LLM client](#7-llm-client)
8. [Sandbox](#8-sandbox)
9. [HTTP surface](#9-http-surface)
10. [Traces and evals](#10-traces-and-evals)
11. [Security and limits](#11-security-and-limits)
12. [Configuration](#12-configuration)
13. [Repository layout](#13-repository-layout)

---

## 1. Processes

| Process | Command | Talks to | Responsibility |
| --- | --- | --- | --- |
| `web` | `uvicorn patchbay.main:app` | SQLite, Redis, (Docker, read-only) | Pages, JSON API, SSE stream. Enqueues runs. |
| `worker` | `celery -A patchbay.worker worker` | SQLite, Redis, Docker, LLM API | Runs `run_session`: creates the sandbox, drives the agent loop, publishes events. |
| `beat` | `celery -A patchbay.worker beat` | Redis | Enqueues the periodic `maintenance` task (reaper + sweeper) on its own queue. |
| `redis` | container | — | Celery broker and result backend; per-session event streams; cancel flags; run leases. |
| `traefik` | container | `web` | Reverse proxy. `web` has no host port, so `docker compose up --scale web=N` works unchanged. |
| sandbox | one container per session | — | Where every tool call executes. Created and destroyed by the worker. |

`web` is stateless: all live state is in Redis and SQLite, which is what allows
several replicas to serve the same session (see [OPERATIONS.md](OPERATIONS.md)). The only Docker access from `web`
is a read-only `git diff` on a running sandbox for the diff panel.

## 2. Lifecycle of a session

```
POST /api/sessions {prompt, repo_url?}
  → sessions row (status=queued), events user.message + session.status
  → Celery message "run_session <id>" on Redis
  → 303 /sessions/<id>; the page opens an SSE connection

worker: run_session(<id>)
  1. lease   SET session:<id>:lock <owner> NX EX 90   (renewed every 30 s while the run is alive;
             duplicate deliveries return immediately; a stale redelivery of a
             finished session is ignored)
  2. status  queued → running
  3. sandbox attach(container_id) if alive, else create + clone repo (or git init)
  4. prompt  first run: system prompt (env facts + workspace tree) + user task;
             later runs: the stored transcript, with any tool calls a crash left
             unanswered given an "interrupted — check before retrying" result
  5. loop    run_agent(...) — see §5; every LLM turn and tool result is
             appended to the transcript and persisted immediately, and usage is
             added to the session totals per turn
  6. end     status ∈ {completed, waiting_for_user, failed, cancelled};
             patch and diff stats stored on the row; container kept alive
  7. release the lease

follow-up / answer to ask_user:
POST /api/sessions/<id>/messages {content}
  → user message appended to the transcript, status=queued, run_session enqueued again
  → the worker reattaches the container and continues the same conversation

stop:
POST /api/sessions/<id>/stop
  → queued: status=cancelled;  running: SET session:<id>:cancel — checked
    before every LLM call and every tool call
```

## 3. Data model

SQLite via SQLAlchemy; the URL is configurable and nothing else is SQLite-specific
except WAL mode and a column-adding migration in `init_db`.

**`sessions`** — one row per session: `id`, `title`, `prompt`, `repo_url`,
`status`, `container_id`, `model`, `prompt_version`, `last_run_id`, running
totals (`steps`, `prompt_tokens`, `completion_tokens`, `cached_tokens`),
`summary`, `error`, `patch`, `diff_files/insertions/deletions`, timestamps.

**`messages`** — the LLM transcript, one row per message, `payload` stored as
JSON exactly as sent to the API, ordered by `seq` (unique per session). A
follow-up loads these and continues; a crash-retry cannot duplicate a
position.

**`events`** — the timeline the UI renders: `id` (global autoincrement),
`session_id`, `type`, `payload` JSON, `created_at`. Indexed on
`(session_id, id)` for replay.

Transcript and timeline are separate on purpose. The transcript must be
exactly what the API accepts (roles, `tool_call_id`s, provider extras); the
timeline carries what the model never sees (durations, exit codes,
truncation flags, status changes).

## 4. Events and streaming

`EventBus.publish(session_id, type, payload)`:

1. `INSERT` into `events` → autoincrement `id`.
2. `XADD session:<id>:events <id>-0 …` to a Redis stream, `MAXLEN ~2000`,
   7-day TTL. The stream entry id *is* the SQLite id.

`GET /api/sessions/<id>/events` (Server-Sent Events):

1. Read `Last-Event-ID` (sent automatically by `EventSource` on reconnect)
   or `?after=`.
2. Replay from SQLite `WHERE id > last`.
3. `XREAD BLOCK 15000` the stream from the same id; on timeout send a
   `: keepalive` comment.
4. Emit `id:` on every frame; close after a terminal `session.status`.

Because both stores share the id, resume after any disconnect is gap-free
and duplicate-free, and a trimmed stream falls back to SQLite.

Event types: `session.status`, `sandbox.ready`, `user.message`,
`agent.message`, `agent.tool_call`, `agent.tool_result`, `agent.ask_user`,
`agent.review`, `llm.usage` (with `run_id`; `role: verifier` for reviews),
`context.compacted`, `diff.updated`, `session.error`.

## 5. Agent loop

`agent/loop.py:run_agent` is a pure function: it takes an LLM client, a
sandbox, the transcript, a tool registry, an `emit` callback, a
`should_cancel` callback and limits, and returns an `AgentOutcome`. The
`agent` package does not import Celery, Redis, HTTP code or the database — not
even transitively — so it runs unchanged in the worker, from the CLI, and in
tests with fakes. `jobs/runner.py` is what wraps it as a job: lease, sandbox,
transcript persistence, events.

```
for step in 1..max_steps:
    if should_cancel():                        → cancelled
    turn = llm.chat(transcript, tools.schemas())   (failure → failed)
    emit(llm.usage); transcript += assistant message
    if turn.content: emit(agent.message)
    if no tool calls:                          → completed
    for call in turn.tool_calls:
        if should_cancel():                    → cancelled (remaining calls answered)
        emit(agent.tool_call)
        if finish and a verifier is set:       (review; see below)
            rejected → the issues become finish's result; next step
        result = tools.run(call, sandbox)      (never raises for tool errors)
        emit(agent.tool_result); transcript += tool message
        finish   → completed, summary = argument
        ask_user → waiting_for_user, question = argument
→ failed (max steps)
```

With `CONTEXT_BUDGET_TOKENS` set, the model is sent a compacted *view* of the
transcript (old tool outputs replaced by a stub, with hysteresis so the cached
prompt prefix stays stable between compactions); the stored transcript is never
shortened. Off by default — see [EVALS.md](EVALS.md).

With `VERIFY_ROUNDS` set, each ending (`finish`, or a reply with no tool
calls) is first reviewed by a separate model call (`agent/verifier.py`): no tools, its own prompt, and as evidence the
user's messages, the agent's summary, the workspace diff and the last command
with its output. It answers `{approve, issues}` in JSON mode, validated with
pydantic (`llm/structured.py`; one retry with the validation error). A
rejection becomes the result of the `finish` call and the loop continues;
after `VERIFY_ROUNDS` rejections the next `finish` is accepted unreviewed. A
review that fails (provider error, unusable JSON) lets the `finish` stand.
Review tokens count against the session budget but not as steps.

Invariants: every tool call gets exactly one tool message, in order, even when
the run stops mid-turn; terminal tools go through the registry like any other;
LLM calls are non-streaming (liveness comes from tool events).

## 6. Tools

A tool is a name, a description written for the model, a JSON-schema for its
arguments, and a `run(sandbox, **args)`. The registry produces the API `tools`
list, dispatches by name, validates arguments against the schema, turns every
failure into an error *result* the model can read, and bounds every output
(head + tail, with an omission marker).

| tool | implementation |
| --- | --- |
| `bash` | `exec` in the container, wrapped in `timeout`; exit code appended to the output |
| `read_file` | numbered lines, 400 per call, paging hint |
| `write_file` | tar over the Docker API; writes confined to `/workspace` |
| `edit_file` | replace a unique exact match; errors explain what to do next |
| `list_files` | pruned `find`, capped |
| `search` | ripgrep with an argv list (no shell interpretation) |
| `ask_user`, `finish` | terminal; they make the two stop conditions explicit calls |

## 7. LLM client

`llm/client.py:OpenAICompatibleClient` wraps the `openai` SDK with a
configurable `base_url`, which covers DeepSeek, Qwen, Kimi, GLM, MiniMax,
Gemini and OpenRouter. It sends the transcript plus tool schemas, and
normalises the reply into `AssistantTurn` (content, tool calls with parsed
arguments and the raw string, usage including cached tokens, latency,
`reasoning_content` when present). Provider-specific fields on tool calls
(e.g. Gemini's `thought_signature`) are kept on the `ToolCall` and echoed back.

Retries: the SDK's backoff for transient errors; on a 429 that names a retry
delay the client waits it out (up to a budget), and fails fast on per-day
quotas.

`LLM_BASE_URL=fake` selects a scripted demo model that exercises the whole
stack against real sandboxes without a key.

## 8. Sandbox

`sandbox/manager.py:Sandbox` — one container per session from
`patchbay-sandbox:latest` (Debian, Python, Node, git, ripgrep, uv, pytest),
started as `sleep infinity`; every tool call is a `docker exec`.

```
docker run -d --name patchbay-<session> --label patchbay.session=<id>
  --user agent --workdir /workspace
  --memory 2g --cpus 2 --pids-limit 512
  --cap-drop ALL --security-opt no-new-privileges [--network none]
then: git clone --depth 50 <url> .   |   git init
```

* `exec(cmd, timeout_s)` — runs `timeout -k 5 <n>s bash -lc …` (or an argv
  list) as `agent`, streams output with a byte cap, returns exit code,
  merged output, duration, `timed_out`.
* `read_file` / `write_file` — `get_archive` / `put_archive` (tar), so file
  contents never pass through a shell; files are owned by `agent`.
* `diff()` / `diff_stat()` — everything changed since `refs/patchbay/base`,
  excluding caches and `node_modules`. The ref is set when the workspace is
  created (the cloned `HEAD`, or git's empty tree for an empty workspace), so
  work the agent commits still appears. Files are staged in a private index
  (`.git/patchbay-index`, reset to the base on each call), so the agent's own
  index is untouched and the current `.gitignore` always applies.
* `export_zip(folder)` — the same staging, then `git archive --format=zip` of
  it: the project as git would keep it, under one top-level folder. The worker
  saves it to `data/projects/<session>.zip` when a run ends (on the shared
  volume, so `web` can serve it without Docker access).
* `attach(container_id)` for follow-ups; `destroy()`; labels for the reaper.

`FakeSandbox` implements the same interface over a dict for tests.

## 9. HTTP surface

| method | path | |
| --- | --- | --- |
| `GET` | `/`, `/sessions/{id}` | pages |
| `POST` | `/api/sessions` | create (JSON or form) and enqueue |
| `GET` | `/api/sessions`, `/api/sessions/{id}`, `/api/sessions/{id}/messages` | list, detail, transcript |
| `GET` | `/api/sessions/{id}/events` | SSE |
| `POST` | `/api/sessions/{id}/messages` | follow-up / answer |
| `POST` | `/api/sessions/{id}/stop` | cancel |
| `DELETE` | `/api/sessions/{id}` | remove the session, its container, Redis keys and trace files (409 while running) |
| `GET` | `/api/sessions/{id}/diff`, `/sessions/{id}/diff.patch` | diff (live if the container exists, else stored), patch download |
| `GET` | `/sessions/{id}/project.zip` | the project as saved at the end of the last run; 404 until a run has ended |
| `GET` | `/api/traces/{run_id}/steps/{n}` | what the model saw at step n |
| `GET` | `/health` | 503 when Redis or the DB is unreachable (Traefik health check) |

## 10. Traces and evals

Every LLM call appends one JSON line to `data/traces/<run_id>.jsonl`: the
full request (messages, tool names), the normalised response, usage, latency,
error. `python -m patchbay.trace <run_id>` prints a run step by step; the UI's
"show prompt" opens the same data for one step.

`evals/` holds a task suite (`tasks/*.yaml`: prompt, optional repo, hidden
tests copied in after the run, a check command) and a runner that reports
pass@1 / pass@k, steps, tokens, cached tokens, cost and wall time per task,
with `--compare` between tagged runs. Prompt versions live in
`agent/prompts/system_v*.md` and are compared there before becoming the default.

## 11. Security and limits

Single-user tool: the worker holds the Docker socket, which is root-equivalent
on the host. Do not expose it publicly as-is.

| concern | mitigation |
| --- | --- |
| arbitrary code | per-session container, non-root, all capabilities dropped, `no-new-privileges`, memory/CPU/pid limits, no host mounts |
| runaway commands | in-container `timeout` (default 120 s, max 600 s); cancel flag between calls |
| runaway agent | `MAX_STEPS` |
| context growth | tool output capped and truncated head+tail; `read_file` paged |
| secrets | the sandbox receives no API key and no host environment |
| network | `SANDBOX_NETWORK=none` for a fully offline sandbox |
| stray containers | `patchbay.session` labels; `make clean-sandboxes` |

Maintenance (celery beat, every `MAINTENANCE_INTERVAL_S`): the reaper removes
sandboxes older than `SANDBOX_TTL_MINUTES` or no longer running and clears
their `container_id`; the sweeper re-queues sessions still marked `running`
whose lease is gone (a dead worker), which then resume from the saved
transcript. `MAX_SESSION_TOKENS` caps a session's spend across runs.

Known gaps: cancel waits for the current command to finish or time out;
prompt injection through repository contents is bounded by the container,
not prevented.

## 12. Configuration

Environment / `.env` (see `.env.example`): `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_THINKING`, `PROMPT_VERSION`, `CONTEXT_BUDGET_TOKENS`,
`VERIFY_ROUNDS`, `REDIS_URL`, `DATABASE_URL`,
`SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_MEMORY`, `SANDBOX_CPUS`,
`SANDBOX_TTL_MINUTES`, `MAX_STEPS`, `MAX_TOOL_OUTPUT_CHARS`,
`DEFAULT_TOOL_TIMEOUT_S`, `MAX_TOOL_TIMEOUT_S`, `TRAEFIK_PORT`.

## 13. Repository layout

```
patchbay/
├── config.py                settings
├── main.py                  FastAPI app
├── worker.py                Celery app and tasks
├── trace.py                 trace viewer CLI
├── db/                      engine, models
├── events/                  event types, bus (SQLite + Redis Streams)
├── sandbox/                 Sandbox, FakeSandbox, protocol
├── llm/                     client, JSON-mode helper, demo model, trace writer, message types
├── agent/                   loop, tools, prompts, context, verifier, cli — no DB, Redis or Celery
├── jobs/                    runner (a session run as a Celery job), maintenance, queue
├── web/                     pages, api, sse stream
├── templates/, static/      UI
evals/                       tasks, runner, report, prices, judge (reviewer vs hidden tests)
sandbox/Dockerfile           sandbox image
tests/unit, tests/integration
```
