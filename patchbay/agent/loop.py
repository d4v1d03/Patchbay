"""Kept free of Celery, HTTP and the database: everything comes in as arguments
and goes out through `emit`, the returned `AgentOutcome` and the transcript.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from patchbay.agent.context import ContextManager
from patchbay.agent.tools import ToolRegistry, ToolResult
from patchbay.agent.verifier import Verifier, gather_evidence
from patchbay.config import get_settings
from patchbay.events.types import (
    EV_ASK_USER,
    EV_CONTEXT,
    EV_ERROR,
    EV_MESSAGE,
    EV_REVIEW,
    EV_TOOL_CALL,
    EV_TOOL_RESULT,
    EV_USAGE,
)
from patchbay.llm.client import LLMClient
from patchbay.llm.types import Message, ToolCall, tool_result_message, user_message
from patchbay.sandbox import SandboxProtocol

EmitFn = Callable[[str, dict[str, Any]], None]

Status = Literal["completed", "waiting_for_user", "failed", "cancelled"]


@dataclass
class Limits:
    max_steps: int = field(default_factory=lambda: get_settings().max_steps)
    max_session_tokens: int = field(default_factory=lambda: get_settings().max_session_tokens)
    tokens_used: int = 0  # budget tokens spent by earlier runs of this session

    def over_budget(self) -> bool:
        return self.max_session_tokens > 0 and self.tokens_used >= self.max_session_tokens


@dataclass
class AgentOutcome:
    status: Status
    steps: int
    summary: str | None = None  # the `finish` summary, or the final assistant text
    question: str | None = None  # the `ask_user` question
    error: str | None = None


def run_agent(
    *,
    llm: LLMClient,
    sandbox: SandboxProtocol,
    transcript: list[Message],
    tools: ToolRegistry,
    emit: EmitFn,
    should_cancel: Callable[[], bool] = lambda: False,
    limits: Limits | None = None,
    context: ContextManager | None = None,
    verifier: Verifier | None = None,
) -> AgentOutcome:
    """Run the model ↔ tools loop until the agent finishes, asks, fails or is cancelled.

    `transcript` is appended to in place (one assistant message per turn, one tool
    message per call) so the caller can persist it after every step. With
    `context` the model is sent a compacted view; with `verifier` each ending is
    reviewed first.
    """
    limits = limits or Limits()
    steps = 0

    for step in range(1, limits.max_steps + 1):
        if should_cancel():
            return AgentOutcome(status="cancelled", steps=steps)
        # checked before a call, not after: a turn already paid for still runs
        if limits.over_budget():
            msg = (
                f"session token budget exhausted "
                f"({limits.tokens_used} >= {limits.max_session_tokens})"
            )
            emit(EV_ERROR, {"message": msg, "recoverable": False})
            return AgentOutcome(status="failed", steps=steps, error=msg)

        view = transcript
        if context is not None:
            view, compaction = context.view(transcript)
            if compaction:
                emit(
                    EV_CONTEXT,
                    {
                        "elided": compaction.elided,
                        "before_tokens": compaction.before_tokens,
                        "after_tokens": compaction.after_tokens,
                    },
                )
        try:
            turn = llm.chat(view, tools.schemas())
        except Exception as e:  # noqa: BLE001
            emit(EV_ERROR, {"message": f"LLM call failed: {e}", "recoverable": False})
            return AgentOutcome(status="failed", steps=steps, error=f"LLM call failed: {e}")
        steps = step
        limits.tokens_used += turn.usage.budget_tokens
        emit(
            EV_USAGE,
            {
                "step": step,
                "prompt_tokens": turn.usage.prompt_tokens,
                "completion_tokens": turn.usage.completion_tokens,
                "cached_tokens": turn.usage.cached_tokens,
                "reasoning_tokens": turn.usage.reasoning_tokens,
                "model": turn.model,
                "latency_ms": turn.latency_ms,
            },
        )

        transcript.append(turn.to_message())
        if turn.content:
            emit(EV_MESSAGE, {"content": turn.content})
        if not turn.tool_calls:
            # a plain reply also ends the run, so it gets the same review as `finish`
            if verifier is not None and verifier.active:
                feedback = _review(
                    verifier, transcript, turn.content or "", sandbox, emit, limits, step
                )
                if feedback:
                    transcript.append(user_message(feedback))
                    continue
            return AgentOutcome(status="completed", steps=steps, summary=turn.content)
        for i, call in enumerate(turn.tool_calls):
            if should_cancel():
                _answer_remaining(transcript, turn.tool_calls[i:], "Cancelled by the user.")
                return AgentOutcome(status="cancelled", steps=steps)

            emit(EV_TOOL_CALL, {"call_id": call.id, "name": call.name, "arguments": call.arguments})
            feedback = None
            if call.name == "finish" and verifier is not None and verifier.active:
                summary = call.arguments.get("summary") or ""
                feedback = _review(verifier, transcript, summary, sandbox, emit, limits, step)
            result = ToolResult(feedback, is_error=True) if feedback else tools.run(call, sandbox)
            emit(
                EV_TOOL_RESULT,
                {
                    "call_id": call.id,
                    "name": call.name,
                    "output": result.output,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "truncated": result.truncated,
                    "is_error": result.is_error,
                },
            )
            transcript.append(tool_result_message(call.id, result.output))

            if feedback:
                _answer_remaining(
                    transcript, turn.tool_calls[i + 1 :], "Skipped: finish was sent back."
                )
                break
            if call.name == "finish":
                _answer_remaining(transcript, turn.tool_calls[i + 1 :], "Skipped: run finished.")
                return AgentOutcome(
                    status="completed", steps=steps, summary=call.arguments.get("summary")
                )
            if call.name == "ask_user":
                question = call.arguments.get("question", "")
                _answer_remaining(
                    transcript, turn.tool_calls[i + 1 :], "Skipped: waiting for the user."
                )
                emit(EV_ASK_USER, {"question": question})
                return AgentOutcome(status="waiting_for_user", steps=steps, question=question)

    msg = f"max steps ({limits.max_steps}) reached without finishing"
    emit(EV_ERROR, {"message": msg, "recoverable": False})
    return AgentOutcome(status="failed", steps=steps, error=msg)


def _review(
    verifier: Verifier,
    transcript: list[Message],
    summary: str,
    sandbox: SandboxProtocol,
    emit: EmitFn,
    limits: Limits,
    step: int,
) -> str | None:
    evidence = gather_evidence(transcript, summary, sandbox)
    review = verifier.review(evidence)
    limits.tokens_used += review.usage.budget_tokens
    if review.usage.total_tokens:
        emit(
            EV_USAGE,
            {
                "step": step,
                "prompt_tokens": review.usage.prompt_tokens,
                "completion_tokens": review.usage.completion_tokens,
                "cached_tokens": review.usage.cached_tokens,
                "reasoning_tokens": review.usage.reasoning_tokens,
                "model": verifier.llm.model,
                "latency_ms": review.latency_ms,
                "role": "verifier",
            },
        )
    emit(
        EV_REVIEW,
        {
            "approve": not review.rejected,
            "issues": review.verdict.issues if review.verdict else [],
            "rejections": verifier.rejections,
            "error": review.error,
        },
    )
    return verifier.feedback(review.verdict) if review.rejected and review.verdict else None


def _answer_remaining(transcript: list[Message], calls: list[ToolCall], note: str) -> None:
    """When we stop mid-turn (cancel / finish / ask_user with more calls queued),
    every remaining tool call still needs a tool message, or the transcript is
    invalid for the next run. Answer them with a short note."""
    for call in calls:
        transcript.append(tool_result_message(call.id, note))


INTERRUPTED_NOTE = (
    "Interrupted: the process running this tool stopped before it reported back, "
    "so whether it ran is unknown. Check the workspace before relying on it or retrying."
)


def repair_transcript(transcript: list[Message]) -> list[Message]:
    """Answer tool calls left dangling at the tail by a crash, in place.

    Every earlier call was answered before the next LLM call was made, so a
    crash can only leave unanswered calls after the last assistant message.
    Returns the messages appended.
    """
    last = next(
        (i for i in range(len(transcript) - 1, -1, -1) if transcript[i]["role"] == "assistant"),
        None,
    )
    if last is None:
        return []
    answered = {m.get("tool_call_id") for m in transcript[last + 1 :] if m["role"] == "tool"}
    added = [
        tool_result_message(tc["id"], INTERRUPTED_NOTE)
        for tc in transcript[last].get("tool_calls") or []
        if tc["id"] not in answered
    ]
    transcript.extend(added)
    return added
