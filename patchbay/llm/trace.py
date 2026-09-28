"""One JSON line per LLM call in data/traces/<run_id>.jsonl: the full request,
the reply, usage and latency.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patchbay.llm.types import AssistantTurn, Message


class TraceWriter:
    def __init__(self, run_id: str, directory: str | Path = "data/traces"):
        self.run_id = run_id
        self.path = Path(directory) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.step = 0

    def record(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        turn: AssistantTurn,
        error: str | None = None,
    ) -> None:
        self.step += 1
        line = {
            "run_id": self.run_id,
            "step": self.step,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "model": turn.model,
            "request": {
                "messages": messages,
                "tools": [t["function"]["name"] for t in tools or []],
            },
            "response": {
                "content": turn.content,
                "reasoning": turn.reasoning,
                "tool_calls": [asdict(tc) for tc in turn.tool_calls],
                "finish_reason": turn.finish_reason,
            },
            "usage": asdict(turn.usage),
            "latency_ms": turn.latency_ms,
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
