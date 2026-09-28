import json
from types import SimpleNamespace

import pytest

from patchbay.llm import AssistantTurn, FakeLLM, ToolCall, Usage, tool_result_message
from patchbay.llm.client import LLMConfigError, OpenAICompatibleClient, parse_response


def _response(*, content=None, tool_calls=None, usage=None, reasoning=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    if reasoning is not None:
        message.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=usage,
        model="provider-model-id",
    )


def _tc(id_, name, arguments):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


def test_parse_text_only():
    turn = parse_response(_response(content="hello"), latency_ms=42, model="m")
    assert turn.content == "hello" and turn.tool_calls == []
    assert turn.latency_ms == 42 and turn.model == "provider-model-id"


def test_parse_tool_calls_and_malformed_json():
    r = _response(
        tool_calls=[
            _tc("c1", "bash", json.dumps({"command": "ls"})),
            _tc("c2", "read_file", '{"path": "a.py"'),  # truncated JSON
            _tc("c3", "finish", "[1, 2]"),  # valid JSON, wrong shape
        ]
    )
    turn = parse_response(r, latency_ms=1, model="m")
    assert [tc.name for tc in turn.tool_calls] == ["bash", "read_file", "finish"]
    assert turn.tool_calls[0].arguments == {"command": "ls"}
    assert turn.tool_calls[1].arguments == {} and turn.tool_calls[1].raw_arguments.startswith(
        '{"path"'
    )
    assert turn.tool_calls[2].arguments == {}


def test_parse_usage_openai_style_cached_tokens():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64),
    )
    turn = parse_response(_response(content="x", usage=usage), latency_ms=1, model="m")
    assert turn.usage == Usage(100, 20, 64)


def test_parse_usage_deepseek_style_cached_tokens():
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_cache_hit_tokens=80)
    turn = parse_response(_response(content="x", usage=usage), latency_ms=1, model="m")
    assert turn.usage.cached_tokens == 80


def test_parse_reasoning_content_roundtrips_into_message():
    turn = parse_response(_response(content="ok", reasoning="thinking..."), latency_ms=1, model="m")
    assert turn.reasoning == "thinking..."
    assert turn.to_message()["reasoning_content"] == "thinking..."


def test_assistant_message_shape_with_tool_calls():
    turn = AssistantTurn(
        content=None,
        tool_calls=[
            ToolCall(
                id="c1", name="bash", arguments={"command": "ls"}, raw_arguments='{"command": "ls"}'
            )
        ],
    )
    msg = turn.to_message()
    assert msg["role"] == "assistant" and msg["content"] is None
    assert msg["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
        }
    ]
    assert tool_result_message("c1", "out") == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "out",
    }


def test_provider_extras_on_tool_calls_roundtrip():
    """Gemini attaches extra_content.google.thought_signature to each tool call
    and rejects the next request unless it is echoed back verbatim."""

    class TC:
        id = "c1"
        type = "function"
        function = SimpleNamespace(name="bash", arguments="{}")

        def model_dump(self):
            return {
                "id": "c1",
                "type": "function",
                "function": {},
                "extra_content": {"google": {"thought_signature": "sig=="}},
            }

    turn = parse_response(_response(tool_calls=[TC()]), latency_ms=1, model="m")
    assert turn.tool_calls[0].extra == {"extra_content": {"google": {"thought_signature": "sig=="}}}
    sent = turn.to_message()["tool_calls"][0]
    assert sent["extra_content"]["google"]["thought_signature"] == "sig=="


def test_usage_addition():
    assert Usage(1, 2, 3) + Usage(10, 20, 30) == Usage(11, 22, 33)
    assert Usage(1, 2).total_tokens == 3


def test_client_refuses_to_construct_without_config():
    with pytest.raises(LLMConfigError):
        OpenAICompatibleClient(base_url="", api_key="", model="")


def test_fake_llm_replays_script_and_records_requests():
    llm = FakeLLM.script(FakeLLM.tool_call("bash", command="ls"), FakeLLM.text("done"))
    t1 = llm.chat([{"role": "user", "content": "hi"}], tools=[{"function": {"name": "bash"}}])
    assert t1.tool_calls[0].name == "bash" and t1.tool_calls[0].arguments == {"command": "ls"}
    t2 = llm.chat([{"role": "user", "content": "hi"}])
    assert t2.content == "done" and t2.tool_calls == []
    assert len(llm.requests) == 2
    with pytest.raises(AssertionError):
        llm.chat([])


def test_fake_llm_tool_call_ids_are_unique_across_turns():
    llm = FakeLLM.script(
        FakeLLM.tool_call("bash", command="a"), FakeLLM.tool_call("bash", command="b")
    )
    ids = {llm.chat([]).tool_calls[0].id, llm.chat([]).tool_calls[0].id}
    assert len(ids) == 2
