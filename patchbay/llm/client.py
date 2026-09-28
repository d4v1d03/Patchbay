"""One client for every OpenAI-compatible provider (DeepSeek, Qwen, Kimi, GLM,
OpenRouter). It sends what it's given and normalises what comes back.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import OpenAI, RateLimitError

from patchbay.config import get_settings
from patchbay.llm.trace import TraceWriter
from patchbay.llm.types import AssistantTurn, Message, ToolCall, Usage

log = logging.getLogger(__name__)


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn: ...


class LLMConfigError(RuntimeError):
    pass


def make_llm(trace: TraceWriter | None = None) -> LLMClient:
    if get_settings().llm_base_url.strip().lower() == "fake":
        from patchbay.llm.demo import DemoLLM

        return DemoLLM(delay_s=get_settings().demo_delay_s, trace=trace)
    return OpenAICompatibleClient.from_settings(trace=trace)


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        thinking: str = "",
        timeout_s: float = 180.0,
        max_retries: int = 3,
        trace: TraceWriter | None = None,
    ):
        if not (base_url and api_key and model):
            raise LLMConfigError(
                "LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must all be set (see .env.example)"
            )
        self.model = model
        self.temperature = temperature
        self.thinking = thinking
        self.trace = trace
        # the SDK retries 429 / 5xx / connection errors itself, with backoff
        self._client = OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    @classmethod
    def from_settings(cls, trace: TraceWriter | None = None) -> OpenAICompatibleClient:
        s = get_settings()
        return cls(
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
            model=s.llm_model,
            temperature=s.llm_temperature,
            thinking=s.llm_thinking,
            trace=trace,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format
        if extra := thinking_params(self.thinking):
            kwargs["extra_body"] = extra

        started = time.monotonic()
        try:
            response = self._create_with_quota_wait(kwargs)
        except Exception as e:
            if self.trace:
                self.trace.record(
                    messages=messages,
                    tools=tools,
                    turn=AssistantTurn(None, model=self.model),
                    error=repr(e),
                )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)

        turn = parse_response(response, latency_ms=latency_ms, model=self.model)
        log.info(
            "llm %s: %d tool call(s), %d+%d tokens (%d cached, %d reasoning), %d ms",
            self.model,
            len(turn.tool_calls),
            turn.usage.prompt_tokens,
            turn.usage.completion_tokens,
            turn.usage.cached_tokens,
            turn.usage.reasoning_tokens,
            latency_ms,
        )
        if self.trace:
            self.trace.record(messages=messages, tools=tools, turn=turn)
        return turn

    # Free tiers enforce per-minute quotas ("retry in 48s"). The SDK's built-in
    # backoff (1 s, 2 s, 4 s) gives up long before that window ends, so on a
    # 429 we honour the provider's suggested wait, up to a total budget.
    QUOTA_WAIT_BUDGET_S = 180.0

    def _create_with_quota_wait(self, kwargs: dict[str, Any]) -> Any:
        waited = 0.0
        while True:
            try:
                return self._client.chat.completions.create(**kwargs)
            except RateLimitError as e:
                if "PerDay" in str(e) or "per day" in str(e).lower():
                    raise  # a daily quota: waiting a minute won't help
                delay = _suggested_retry_delay(str(e)) or 20.0
                if waited + delay > self.QUOTA_WAIT_BUDGET_S:
                    raise
                log.warning("rate limited by %s; waiting %.0fs", self.model, delay)
                time.sleep(delay)
                waited += delay


def thinking_params(level: str) -> dict[str, Any]:
    level = level.strip().lower()
    if not level:
        return {}
    if level == "off":
        return {"thinking": {"type": "disabled"}}
    return {"reasoning_effort": level}


def _suggested_retry_delay(message: str) -> float | None:
    """Pull 'retry in 48.5s' / 'retryDelay: 49s' style hints out of a 429 body."""
    m = re.search(r"retry(?:Delay)?[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)\s*s", message, re.IGNORECASE)
    return min(float(m.group(1)) + 1.0, 90.0) if m else None


def parse_response(response: Any, *, latency_ms: int, model: str) -> AssistantTurn:
    """Normalise a chat.completions response into an AssistantTurn.

    Provider quirks handled here so nothing else has to know about them:
    - tool call arguments arrive as a JSON *string*; malformed JSON is kept
      raw (with `{}` parsed) so the loop can report it back to the model.
    - cached-token counts live in different places per provider.
    - thinking models add `reasoning_content` to the message.
    """
    choice = response.choices[0]
    msg = choice.message

    tool_calls: list[ToolCall] = []
    for tc in msg.tool_calls or []:
        raw = tc.function.arguments or ""
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        # Anything beyond id/type/function is provider-specific and must round-trip.
        dump = tc.model_dump() if hasattr(tc, "model_dump") else {}
        extra = {k: v for k, v in dump.items() if k not in ("id", "type", "function") and v}
        tool_calls.append(
            ToolCall(
                id=tc.id, name=tc.function.name, arguments=args, raw_arguments=raw, extra=extra
            )
        )

    usage = Usage()
    if response.usage:
        u = response.usage
        details = getattr(u, "prompt_tokens_details", None)
        cached = (
            getattr(details, "cached_tokens", None)  # OpenAI / Kimi / Qwen style
            or getattr(u, "prompt_cache_hit_tokens", None)  # DeepSeek style
            or 0
        )
        out_details = getattr(u, "completion_tokens_details", None)
        usage = Usage(
            prompt_tokens=u.prompt_tokens or 0,
            completion_tokens=u.completion_tokens or 0,
            cached_tokens=int(cached),
            reasoning_tokens=int(getattr(out_details, "reasoning_tokens", None) or 0),
        )

    return AssistantTurn(
        content=msg.content,
        tool_calls=tool_calls,
        usage=usage,
        latency_ms=latency_ms,
        model=getattr(response, "model", None) or model,
        finish_reason=choice.finish_reason,
        reasoning=getattr(msg, "reasoning_content", None) or None,
    )


# ---------------------------------------------------------------------- fake


@dataclass
class FakeLLM:
    """Replays scripted turns in order. Records every request for assertions.

    llm = FakeLLM.script(
        FakeLLM.tool_call("bash", command="ls"),
        FakeLLM.tool_call("finish", summary="done"),
    )
    """

    turns: list[AssistantTurn] = field(default_factory=list)
    model: str = "fake"
    requests: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 0

    @classmethod
    def script(cls, *turns: AssistantTurn) -> FakeLLM:
        return cls(turns=list(turns))

    @staticmethod
    def tool_call(name: str, /, **arguments: Any) -> AssistantTurn:
        return FakeLLM.tool_calls((name, arguments))

    @staticmethod
    def tool_calls(*calls: tuple[str, dict[str, Any]], content: str | None = None) -> AssistantTurn:
        tcs = [
            ToolCall(
                id=f"call_{i}_{name}", name=name, arguments=args, raw_arguments=json.dumps(args)
            )
            for i, (name, args) in enumerate(calls)
        ]
        return AssistantTurn(content=content, tool_calls=tcs, usage=Usage(10, 5), model="fake")

    @staticmethod
    def text(content: str) -> AssistantTurn:
        return AssistantTurn(content=content, usage=Usage(10, 5), model="fake")

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> AssistantTurn:
        self.requests.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "response_format": response_format,
            }
        )
        if not self.turns:
            raise AssertionError(
                "FakeLLM: script exhausted — the loop asked for more turns than scripted"
            )
        turn = self.turns.pop(0)
        # unique ids, so transcripts stay valid across turns
        if turn.tool_calls:
            self._next_id += 1
            turn = AssistantTurn(
                content=turn.content,
                tool_calls=[
                    ToolCall(f"call_{self._next_id}_{i}", tc.name, tc.arguments, tc.raw_arguments)
                    for i, tc in enumerate(turn.tool_calls)
                ],
                usage=turn.usage,
                model=turn.model,
            )
        return turn
