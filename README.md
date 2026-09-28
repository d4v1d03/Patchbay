# Patchbay

A self-hosted AI coding agent. Give it a task and optionally a public git repo;
it starts a Docker sandbox, writes code, runs the tests, fixes what fails, and
streams every command and its output back to you until it's done or you stop it.

No agent framework: the loop, the sandbox and the LLM client are one file each.
It works with any OpenAI-compatible provider (DeepSeek, Qwen, Kimi, GLM, Gemini,
OpenRouter).

## What it does

- Takes a task in plain English, against a cloned repo or an empty workspace.
- Runs everything in a fresh container per session: non-root, all Linux
  capabilities dropped, memory/CPU/pid limits, no host mounts, no secrets.
- Gives the model eight tools: `bash`, `read_file`, `write_file`, `edit_file`,
  `list_files`, `search`, `ask_user`, `finish`.
- Streams each model turn, tool call and output live: printed by the CLI,
  over SSE in the web UI.
- Logs every LLM call as JSONL: the exact request, the reply, tokens, cached
  tokens and latency.
- Shows the diff of everything changed since the session started, and lets you
  download it as a patch, or the whole project as a zip (what git would keep:
  no `node_modules`, nothing `.gitignore` excludes).

## How a run works

```
task ──▶ system prompt (static) + first user message (repo, workspace tree, task)
              │
              ▼
        ┌─ llm.chat(transcript, tool schemas) ──▶ model replies with tool calls
        │        │
        │        ▼
        │   registry.run(call, sandbox) ──▶ docker exec / tar in / tar out
        │        │
        │        ▼
        │   tool result appended to transcript, event emitted
        └────────┘  repeat until `finish`, `ask_user`, cancel, or max_steps
```

The model doesn't run anything itself; it asks for a tool call and the worker
runs it inside the container. Tool output is capped (head and tail kept, the
middle cut with a marker) before it goes back into the context.

## Architecture

```
 Browser ──HTML/JSON──▶ ┌──────────────┐          ┌───────────────┐
   ▲                    │  web         │  enqueue │  worker        │
   │  SSE (live events) │  FastAPI +   │ ───────▶ │  Celery        │
   └────────────────────│  Jinja2      │          │                │
                        └──────┬───────┘          │  agent loop    │
                               │                  │   ├─ LLM call ─┼──▶ any OpenAI-compatible API
                        ┌──────▼───────┐  events  │   └─ tools ────┼──▶ ┌─────────────────────┐
                        │  Redis       │ ◀─────── │                │    │  sandbox container  │
                        │  broker +    │          └───────┬────────┘    │  /workspace (repo)  │
                        │  event stream│                  │ docker API  │  bash, git, py, node│
                        └──────────────┘                  └────────────▶│  non-root, limited  │
                        ┌──────────────┐                                └─────────────────────┘
                        │  SQLite      │  sessions · transcript · events
                        └──────────────┘
```

* `web` serves pages, the JSON API and the SSE stream. It never touches Docker or the LLM.
* `worker` runs one Celery task per agent run: creates the sandbox, drives the
  loop, publishes events. `acks_late` + resumable transcripts mean a crashed
  worker's run is picked up, not lost.
* Redis is the Celery broker and carries a per-session event stream that the
  SSE endpoint tails (replay via `Last-Event-ID`).
* SQLite holds sessions, the LLM transcript, and the event timeline.
* A Traefik reverse proxy fronts `web`, so `docker compose up --scale web=3`
  works unchanged.

## Sandbox guarantees

| Concern | Mitigation |
| --- | --- |
| Agent runs arbitrary code | One container per session; user `agent` (uid 1000); `--cap-drop ALL`; `no-new-privileges`; memory / CPU / pids limits; no host mounts |
| Runaway commands | Wrapped in the container's own `timeout` (default 120 s, max 600 s) |
| Runaway agent | `MAX_STEPS` per run and `MAX_SESSION_TOKENS` per session; hitting either ends the run with an error |
| Context blow-up | Tool output capped and truncated head+tail; `read_file` paged at 400 lines |
| Secrets | The sandbox receives no API key and no host environment |
| Network exfiltration | `SANDBOX_NETWORK=none` turns the sandbox fully offline |
| Stray containers | Every container is labelled; a beat-scheduled reaper removes idle ones after `SANDBOX_TTL_MINUTES`; `make clean-sandboxes` for the rest |
| Dead worker mid-run | 90 s leases renewed by a heartbeat; per-turn transcript and usage writes; a sweeper re-queues orphaned sessions, which resume from the saved transcript (verified by killing a worker mid-run) |

This is a single-user dev tool: the worker holds the Docker socket, which is
root-equivalent on the host. Don't expose it to the internet as-is.

## Quickstart

```bash
cp .env.example .env            # set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
make build-sandbox              # the image agents run in
docker compose up               # redis + traefik + web + worker + beat → http://localhost:8088
```

No API key yet? `LLM_BASE_URL=fake` runs a scripted demo model against real
sandboxes, enough to try the UI end to end.

Run the agent from the terminal, no web stack needed:

```bash
docker compose up -d redis
uv sync
uv run python -m patchbay.agent.cli "Add a --version flag and a test for it" --repo https://github.com/you/repo
uv run python -m patchbay.agent.cli "Create a CSV→JSON CLI with pytest tests and make them pass"
```

Every run writes `data/traces/<run_id>.jsonl` — the exact prompt the model saw at
each step, its reply, tokens and latency.

## Configuration

| var | default | |
| --- | --- | --- |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | — | any OpenAI-compatible endpoint |
| `LLM_TEMPERATURE` | `0.2` | |
| `LLM_THINKING` | *(provider default)* | `off`, `low`, `high` or `max`; DeepSeek models think before every reply unless this is `off` |
| `PROMPT_VERSION` | `v2` | system prompt version; compared on the eval suite before becoming the default |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `DATABASE_URL` | `sqlite:///data/patchbay.db` | |
| `SANDBOX_IMAGE` | `patchbay-sandbox:latest` | |
| `SANDBOX_NETWORK` | `bridge` | `none` for offline |
| `SANDBOX_MEMORY` / `SANDBOX_CPUS` | `2g` / `2` | |
| `SANDBOX_TTL_MINUTES` | `60` | idle containers are reaped after this |
| `MAX_STEPS` | `40` | LLM calls per run |
| `CONTEXT_BUDGET_TOKENS` | `0` | compact old tool outputs past this size; `0` = off (it cost more than it saved, see docs/EVALS.md) |
| `VERIFY_ROUNDS` | `0` | review each `finish` with a second model call and send the work back at most N times; `0` = off |
| `MAX_SESSION_TOKENS` | `400000` | tokens per session across runs, cached ones counting a tenth; `0` = unlimited |
| `MAINTENANCE_INTERVAL_S` | `60` | reaper / sweeper cadence |
| `MAX_TOOL_OUTPUT_CHARS` | `8000` | |
| `DEFAULT_TOOL_TIMEOUT_S` / `MAX_TOOL_TIMEOUT_S` | `120` / `600` | |
| `TRAEFIK_PORT` | `8088` | host port for the compose stack |

## Layout

```
patchbay/
├── config.py            settings
├── main.py              FastAPI app · web/ (pages, api, sse)
├── worker.py            Celery app and tasks
├── db/                  SQLAlchemy engine · models
├── sandbox/             Sandbox (Docker) · FakeSandbox · SandboxProtocol
├── llm/                 OpenAI-compatible client · JSON-mode helper · FakeLLM · demo model · traces
├── agent/               loop · tools · prompts · context · verifier · cli (no DB, Redis or Celery)
└── jobs/                runner · maintenance · queue: the agent as a Celery job
evals/                   task suite with hidden tests · runner · report · reviewer scoring
tools/                   SSE resume client · worker-kill script
sandbox/Dockerfile       the image agents run in
tests/unit               FakeLLM + FakeSandbox, no Docker needed
tests/integration        real containers (skipped when no daemon)
```

## Development

```bash
make test        # unit tests
make test-all    # + integration tests against real containers
make lint        # ruff
make web         # uvicorn with reload on :8000
make worker      # celery worker
make beat        # periodic reaper / sweeper

uv run python evals/run.py --trials 3 --tag mine    # eval suite; needs a real model
uv run python evals/report.py --compare baseline mine
```

## Status

**Shipped in v1**

| | Capability | Proof |
| --- | --- | --- |
| ✅ | Sandbox per session | non-root, all capabilities dropped, CPU, memory and process limits |
| ✅ | Tool-calling agent loop | 8 tools; built a tested React app in 11 steps and 77 s |
| ✅ | Your repo in, a patch or zip out | diff against the session's start, even after the agent commits |
| ✅ | Live event stream | Redis Streams and SSE; no gaps after a web replica was killed |
| ✅ | Prompt tracing | every model call logged with the exact prompt it saw |
| ✅ | Eval suite | hidden tests and 95% intervals; about 450 runs for under $1 |
| ✅ | Cache-aware context | 88% of a new session's first request served from cache |
| ✅ | LLM-as-judge reviewer | 20/20 planted bugs caught, 42/42 correct results approved |
| ✅ | Crash recovery | worker killed mid-run; the run resumed and finished |
| ✅ | Any OpenAI-compatible model | thinking off measured 20% cheaper at the same pass rate |

**Roadmap**

| | Capability | What it adds |
| --- | --- | --- |
| 🔜 v1.1 | Model router | a model per role, failover between providers |
| 🔜 v1.1 | Cross-model review | the reviewer from a different lab |
| 🔜 v1.1 | Planning step | a structured plan, challenged by a critic model |
| 🔜 v1.2 | Repo memory | what it learns about a repo carries over |
| 🔜 v1.2 | Parallel sub-agents | for exploring and building |
| 🔜 v1.2 | Browser QA | clicks through the app and records it |
| 🔜 v2 | Triggered sessions | from a schedule, a webhook or an alert |
| 🔜 v2 | Pull requests | opened through a GitHub App |
| 🔜 v2 | Integrations over MCP | Slack, Linear, Sentry |
| 🔜 v2 | MicroVM sandboxes | that run the full stack |

## Deploying

`docker-compose.prod.yml` adds HTTPS (Let's Encrypt) and a password login for
running it on a public VM, and `docker-compose.tunnel.yml` does the same behind a
tunnel from your own machine; [deploy/README.md](deploy/README.md) has the steps
for Tailscale Funnel and for a free Oracle Cloud instance.

## Docs

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — processes, session lifecycle, data model, streaming, agent loop, sandbox, HTTP surface.
* [docs/DECISIONS.md](docs/DECISIONS.md) — architecture decision records.
* [docs/EVALS.md](docs/EVALS.md) — the eval suite and the measurements: prompts, caching, compaction, thinking, the reviewer.
* [docs/OPERATIONS.md](docs/OPERATIONS.md) — running it scaled: replicas, workers, sandboxes, and what was verified.
* [docs/ROADMAP.md](docs/ROADMAP.md) — what's done, what's next.

## License

MIT
