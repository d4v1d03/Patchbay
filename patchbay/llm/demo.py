"""Scripted model for LLM_BASE_URL=fake: real sandbox and tools, no tokens spent."""

from __future__ import annotations

import json
import time
from typing import Any

from patchbay.llm.trace import TraceWriter
from patchbay.llm.types import AssistantTurn, Message, ToolCall, Usage

HELLO = """\
def hello(name: str = "patchbay") -> str:
    return f"hello, {name}"


if __name__ == "__main__":
    print(hello())
"""
TEST = """\
from hello import hello


def test_hello():
    assert hello() == "hello, patchbay"
    assert hello("you") == "hello, you"
"""
FOLLOW_UP_SUMMARY = "Checked the workspace after your message; nothing else to change."
DONE_SUMMARY = "Added hello.py and tests/test_hello.py; `python3 -m pytest -q` → 1 passed."


class DemoLLM:
    model = "demo"

    def __init__(self, delay_s: float = 0.6, trace: TraceWriter | None = None):
        self.delay_s = delay_s
        self.trace = trace
        self._n = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        time.sleep(self.delay_s)
        if response_format:  # a review of the work: the demo always approves
            return AssistantTurn(
                content=json.dumps({"approve": True, "issues": []}),
                usage=Usage(prompt_tokens=300, completion_tokens=12),
                model=self.model,
            )
        self._n += 1
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), {"content": ""})
        # turns since the most recent user message
        idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
        k = sum(1 for m in messages[idx:] if m["role"] == "assistant")
        follow_up = sum(1 for m in messages if m["role"] == "user") > 1
        task = (last_user.get("content") or "").lower()

        if follow_up:
            script = [
                (None, [("bash", {"command": "ls -la && git status --short"})]),
                (
                    "Done with the follow-up.",
                    [
                        (
                            "finish",
                            {"summary": FOLLOW_UP_SUMMARY},
                        )
                    ],
                ),
            ]
        elif "ask" in task:
            script = [
                ("Let me look at the workspace first.", [("list_files", {})]),
                (
                    None,
                    [
                        (
                            "ask_user",
                            {"question": "Which database should I target: SQLite or PostgreSQL?"},
                        )
                    ],
                ),
            ]
        else:
            script = [
                ("Let me look at the workspace first.", [("list_files", {})]),
                (None, [("write_file", {"path": "hello.py", "content": HELLO})]),
                (None, [("write_file", {"path": "tests/test_hello.py", "content": TEST})]),
                (None, [("bash", {"command": "python3 -m pytest -q"})]),
                (
                    None,
                    [
                        (
                            "finish",
                            {"summary": DONE_SUMMARY},
                        )
                    ],
                ),
            ]
        content, calls = script[min(k, len(script) - 1)]
        tool_calls = [
            ToolCall(
                id=f"demo_{self._n}_{i}", name=name, arguments=args, raw_arguments=json.dumps(args)
            )
            for i, (name, args) in enumerate(calls)
        ]
        prompt_tokens = 900 + 350 * len(messages)
        turn = AssistantTurn(
            content=content,
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt_tokens, completion_tokens=40, cached_tokens=prompt_tokens // 3
            ),
            latency_ms=int(self.delay_s * 1000),
            model=self.model,
            finish_reason="tool_calls",
        )
        if self.trace:
            self.trace.record(messages=messages, tools=tools, turn=turn)
        return turn
