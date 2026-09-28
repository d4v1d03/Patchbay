"""Keeps what the model sees within a token budget.

The stored transcript is never changed. The model gets a view of it where tool
results older than the last few turns are replaced by a short stub once the
conversation passes the budget.

Providers cache by prefix, so the view only changes in jumps: compaction
triggers at the budget and goes down to half of it, and a stub stays
byte-identical from then on. Assistant messages are never touched; some
providers reject an edited one (signed tool calls).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from patchbay.llm.types import Message

CHARS_PER_TOKEN = 4  # rough, provider-independent; deterministic on purpose
MESSAGE_OVERHEAD_TOKENS = 4
STUB_NOTE = (
    "[… {n} chars elided to save context. Re-run the command or re-read the file "
    "if you need this output again.]"
)


def estimate_tokens(messages: list[Message]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            chars += len(json.dumps(tc.get("function", {})))
    return chars // CHARS_PER_TOKEN + MESSAGE_OVERHEAD_TOKENS * len(messages)


@dataclass(frozen=True)
class ContextPolicy:
    budget_tokens: int = 0  # 0 = never compact
    keep_recent_turns: int = 3  # the newest N assistant turns keep their tool results whole
    low_water_ratio: float = 0.5  # after a jump, aim for this fraction of the budget
    stub_head_chars: int = 160  # a stub keeps the first few lines, so the model recalls what it was

    @property
    def enabled(self) -> bool:
        return self.budget_tokens > 0


@dataclass(frozen=True)
class Compaction:
    elided: int  # tool results newly stubbed in this jump
    before_tokens: int
    after_tokens: int


class ContextManager:
    def __init__(self, policy: ContextPolicy | None = None):
        self.policy = policy or ContextPolicy()
        # tool results at indices below this are stubbed; it only moves forward
        self.elide_before = 0

    def view(self, transcript: list[Message]) -> tuple[list[Message], Compaction | None]:
        """What to send the model, and the compaction that just happened (if any)."""
        if not self.policy.enabled:
            return transcript, None
        current = self._render(transcript, self.elide_before)
        before = estimate_tokens(current)
        if before <= self.policy.budget_tokens:
            return current, None

        # stub oldest-first until under the low-water target, never into the last K turns
        limit = self._protected_from(transcript)
        target = int(self.policy.budget_tokens * self.policy.low_water_ratio)
        boundary, tokens, newly = self.elide_before, before, 0
        while boundary < limit and tokens > target:
            m = transcript[boundary]
            if self._worth_stubbing(m):
                tokens -= estimate_tokens([m]) - estimate_tokens([self._stub(m)])
                newly += 1
            boundary += 1
        if newly == 0:
            return current, None  # nothing old enough left to elide
        self.elide_before = boundary
        compacted = self._render(transcript, boundary)
        return compacted, Compaction(newly, before, estimate_tokens(compacted))

    # ------------------------------------------------------------------ internals

    def _protected_from(self, transcript: list[Message]) -> int:
        """Index of the Kth most recent assistant message; everything from here
        on is kept whole."""
        seen = 0
        for i in range(len(transcript) - 1, -1, -1):
            if transcript[i]["role"] == "assistant":
                seen += 1
                if seen == self.policy.keep_recent_turns:
                    return i
        return 0

    def _worth_stubbing(self, m: Message) -> bool:
        return (
            m["role"] == "tool" and len(m.get("content") or "") > self.policy.stub_head_chars + 200
        )

    def _render(self, transcript: list[Message], elide_before: int) -> list[Message]:
        if elide_before <= 0:
            return transcript
        out: list[Message] = []
        for i, m in enumerate(transcript):
            if i < elide_before and self._worth_stubbing(m):
                out.append(self._stub(m))
            else:
                out.append(m)
        return out

    def _stub(self, m: Message) -> Message:
        content = m.get("content") or ""
        head = content[: self.policy.stub_head_chars].rstrip()
        return {**m, "content": f"{head}\n{STUB_NOTE.format(n=len(content))}"}


def from_settings(budget_tokens: int | None = None) -> ContextManager:
    from patchbay.config import get_settings

    st = get_settings()
    return ContextManager(
        ContextPolicy(
            budget_tokens=st.context_budget_tokens if budget_tokens is None else budget_tokens,
            keep_recent_turns=st.context_keep_turns,
        )
    )
