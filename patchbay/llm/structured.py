"""JSON mode guarantees the reply parses, not that it has the right fields, so
replies are validated with pydantic and the error is sent back once.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from patchbay.llm.client import LLMClient
from patchbay.llm.types import Message, Usage, user_message

JSON_MODE = {"type": "json_object"}


class StructuredOutputError(RuntimeError):
    def __init__(self, message: str, usage: Usage):
        super().__init__(message)
        self.usage = usage  # the failed attempts still cost tokens


def chat_json[T: BaseModel](
    llm: LLMClient, messages: list[Message], schema: type[T], *, retries: int = 1
) -> tuple[T, Usage]:
    msgs = list(messages)
    usage = Usage()
    error = ""
    for _ in range(retries + 1):
        turn = llm.chat(msgs, response_format=JSON_MODE)
        usage += turn.usage
        raw = turn.content or ""
        try:
            return schema.model_validate_json(raw), usage
        except ValidationError as e:
            error = _short(e)
            msgs += [
                {"role": "assistant", "content": raw},
                user_message(f"That reply was not valid: {error}. Reply with the JSON only."),
            ]
    tries = retries + 1
    raise StructuredOutputError(f"no valid {schema.__name__} after {tries} tries: {error}", usage)


def _short(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'reply'}: {err['msg']}" for err in e.errors()[:3]
    )
