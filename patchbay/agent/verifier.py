"""Reviews the end of a run before it's accepted.

A separate model call (no tools, its own prompt) sees the request, the
summary, the diff and the last command's output, and returns {approve,
issues}. A rejection goes back to the agent and the run continues. After
max_rejections the next ending is accepted unreviewed, and a review that
fails lets the ending stand, so a wrong or broken reviewer can cost rounds
but can't trap or fail a run.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, model_validator

from patchbay.agent.prompts import load_prompt
from patchbay.agent.tools import truncate_middle
from patchbay.llm.client import LLMClient
from patchbay.llm.structured import StructuredOutputError, chat_json
from patchbay.llm.types import Message, Usage, system_message, user_message
from patchbay.sandbox import SandboxProtocol

MAX_DIFF_CHARS = 12_000
MAX_OUTPUT_CHARS = 3_000


class Verdict(BaseModel):
    approve: bool
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rejection_says_why(self) -> Verdict:
        if not self.approve and not self.issues:
            raise ValueError("a rejection must list at least one issue")
        return self


@dataclass(frozen=True)
class Evidence:
    request: str
    summary: str
    diff: str
    last_command: str | None = None
    last_output: str | None = None

    def render(self) -> str:
        parts = [
            f"## Request\n{self.request}",
            f"## Agent's summary\n{self.summary or '(none)'}",
            f"## Diff\n```diff\n{self.diff}\n```",
        ]
        if self.last_command:
            parts.append(
                f"## Last command\n```\n$ {self.last_command}\n{self.last_output or ''}\n```"
            )
        else:
            parts.append("## Last command\n(the agent ran no commands)")
        return "\n\n".join(parts)


@dataclass
class Review:
    verdict: Verdict | None  # None: the review failed and the finish stands
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    error: str | None = None

    @property
    def rejected(self) -> bool:
        return self.verdict is not None and not self.verdict.approve


class Verifier:
    def __init__(self, llm: LLMClient, *, max_rejections: int = 2, prompt: str = "verifier_v1"):
        self.llm = llm
        self.max_rejections = max_rejections
        self.system = load_prompt(prompt)
        self.rejections = 0

    @property
    def active(self) -> bool:
        return self.rejections < self.max_rejections

    def review(self, evidence: Evidence) -> Review:
        messages = [system_message(self.system), user_message(evidence.render())]
        started = time.monotonic()
        try:
            verdict, usage = chat_json(self.llm, messages, Verdict)
        except StructuredOutputError as e:
            return Review(None, e.usage, _ms_since(started), str(e))
        except Exception as e:  # noqa: BLE001 — a provider error must not fail the run
            return Review(None, Usage(), _ms_since(started), f"review failed: {e}")
        if not verdict.approve:
            self.rejections += 1
        return Review(verdict, usage, _ms_since(started))

    @staticmethod
    def feedback(verdict: Verdict) -> str:
        issues = "\n".join(f"- {i}" for i in verdict.issues)
        return (
            "Not accepted yet. A reviewer found problems:\n"
            f"{issues}\n"
            "Fix the ones that are right and call finish again. If an issue is mistaken, "
            "call finish again and say why in the summary."
        )


def gather_evidence(transcript: list[Message], summary: str, sandbox: SandboxProtocol) -> Evidence:
    request = "\n\n---\n\n".join(
        m["content"] for m in transcript if m["role"] == "user" and m.get("content")
    )
    try:
        diff = sandbox.diff() or "(no changes)"
    except Exception as e:  # noqa: BLE001
        diff = f"(could not read the diff: {e})"
    command, output = last_command(transcript)
    return Evidence(
        request=request,
        summary=summary,
        diff=truncate_middle(diff, MAX_DIFF_CHARS)[0],
        last_command=command,
        last_output=truncate_middle(output, MAX_OUTPUT_CHARS)[0] if output else output,
    )


def last_command(transcript: list[Message]) -> tuple[str | None, str | None]:
    results = {m["tool_call_id"]: m.get("content") or "" for m in transcript if m["role"] == "tool"}
    for m in reversed(transcript):
        for tc in reversed(m.get("tool_calls") or []):
            fn = tc.get("function", {})
            if fn.get("name") != "bash" or tc["id"] not in results:
                continue
            try:
                command = json.loads(fn.get("arguments") or "{}").get("command")
            except (json.JSONDecodeError, AttributeError):
                command = fn.get("arguments")
            return command, results[tc["id"]]
    return None, None


def from_settings(llm: LLMClient, rounds: int | None = None) -> Verifier | None:
    from patchbay.config import get_settings

    rounds = get_settings().verify_rounds if rounds is None else rounds
    return Verifier(llm, max_rejections=rounds) if rounds > 0 else None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
