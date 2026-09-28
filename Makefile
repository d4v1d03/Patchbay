.DEFAULT_GOAL := help
.PHONY: help install dev-redis web worker build-sandbox test test-all lint fmt up down clean-sandboxes

help:                 ## show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:              ## create .venv and install all deps (incl. dev)
	uv sync

dev-redis:            ## start only redis (for local uv run development)
	docker compose up -d redis

web:                  ## run the FastAPI app with reload on :8000
	uv run uvicorn patchbay.main:app --reload --port 8000

worker:               ## run a Celery worker (runs + maintenance queues)
	uv run celery -A patchbay.worker worker -l info -Q runs,maintenance

beat:                 ## run the periodic scheduler (reaper, sweeper)
	uv run celery -A patchbay.worker beat -l info

build-sandbox:        ## build the sandbox image agents run in
	docker build -t patchbay-sandbox:latest sandbox/

test:                 ## unit tests (no Docker needed)
	uv run pytest tests/unit -q

test-all:             ## unit + integration tests (needs Docker)
	uv run pytest -q

lint:                 ## ruff check
	uv run ruff check .

fmt:                  ## ruff format + fix imports
	uv run ruff format . && uv run ruff check --fix .

up:                   ## full stack via compose (http://localhost:8088)
	docker compose up --build

down:                 ## stop the stack
	docker compose down

clean-sandboxes:      ## remove every sandbox container patchbay created
	docker ps -aq --filter label=patchbay.session | xargs -r docker rm -f
