import copy

from patchbay.agent.context import (
    STUB_NOTE,
    ContextManager,
    ContextPolicy,
    estimate_tokens,
)
from patchbay.llm.types import system_message, tool_result_message, user_message


def _turn(i: int, output_chars: int) -> list[dict]:
    call = {"id": f"c{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    return [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        tool_result_message(f"c{i}", f"output of step {i}\n" + "x" * output_chars),
    ]


def _transcript(turns: int, output_chars: int = 4000) -> list[dict]:
    t = [system_message("SYSTEM " * 50), user_message("do the task")]
    for i in range(turns):
        t += _turn(i, output_chars)
    return t


def _stubbed(m: dict) -> bool:
    return m["role"] == "tool" and "chars elided" in m["content"]


def test_estimate_is_roughly_chars_over_four():
    msgs = [user_message("a" * 400)]
    assert 100 <= estimate_tokens(msgs) <= 110


def test_disabled_returns_the_transcript_itself():
    t = _transcript(10)
    view, c = ContextManager(ContextPolicy(budget_tokens=0)).view(t)
    assert view is t and c is None


def test_under_budget_is_untouched():
    t = _transcript(2, output_chars=100)
    view, c = ContextManager(ContextPolicy(budget_tokens=100_000)).view(t)
    assert view == t and c is None


def test_over_budget_stubs_old_tool_results_only():
    t = _transcript(8)  # ~8k tokens of tool output
    original = copy.deepcopy(t)
    mgr = ContextManager(ContextPolicy(budget_tokens=3000, keep_recent_turns=3))
    view, c = mgr.view(t)

    assert t == original  # the stored transcript is never modified
    assert c is not None and c.elided == 5 and c.after_tokens < c.before_tokens
    # system, task and every assistant message are byte-identical
    assert view[0] == t[0] and view[1] == t[1]
    assert [m for m in view if m["role"] == "assistant"] == [
        m for m in t if m["role"] == "assistant"
    ]
    tools = [m for m in view if m["role"] == "tool"]
    assert [_stubbed(m) for m in tools] == [True] * 5 + [False] * 3  # last 3 turns whole
    # stubs keep the head and the call id, so the transcript stays valid
    assert tools[0]["content"].startswith("output of step 0")
    assert tools[0]["tool_call_id"] == "c0"
    assert STUB_NOTE.format(n=len(t[3]["content"])) in tools[0]["content"]


def test_view_is_stable_between_jumps_so_the_prefix_cache_hits():
    t = _transcript(16, output_chars=2000)  # ~8.4k tokens
    mgr = ContextManager(ContextPolicy(budget_tokens=8000, keep_recent_turns=3))
    first, c = mgr.view(t)
    assert c is not None and c.after_tokens <= 4000  # compacted down to the low-water mark
    t += _turn(16, output_chars=2000)  # the next step: headroom left, no new jump
    second, c = mgr.view(t)
    assert c is None
    assert second[: len(first)] == first  # every earlier message byte-identical


def test_hysteresis_keeps_compactions_rare_at_realistic_budgets():
    t = _transcript(4, output_chars=2000)
    mgr = ContextManager(ContextPolicy(budget_tokens=12_000, keep_recent_turns=3))
    compacted_at = []
    for step in range(40):
        t += _turn(100 + step, output_chars=2000)
        _, c = mgr.view(t)
        if c:
            compacted_at.append(step)
    assert 2 <= len(compacted_at) <= 6
    assert all(b - a > 5 for a, b in zip(compacted_at, compacted_at[1:], strict=False))


def test_masking_degrades_to_a_tail_window_when_the_floor_is_high():
    """Stubs and assistant messages can't be shrunk further, so their total
    grows with every turn. Once it passes the low-water mark, each step stubs
    just the result leaving the recent window — at the tail, so the early
    prefix stays byte-identical and still caches."""
    t = _transcript(4, output_chars=2000)
    mgr = ContextManager(ContextPolicy(budget_tokens=4000, keep_recent_turns=3))
    prev_view, prev_boundary = None, 0
    for step in range(40):
        t += _turn(100 + step, output_chars=2000)
        view, c = mgr.view(t)
        if step >= 30:
            assert c is not None and c.elided == 1  # a sliding window, one stub per step
            assert view[:prev_boundary] == prev_view[:prev_boundary]
        prev_view, prev_boundary = view, mgr.elide_before


def test_boundary_only_moves_forward_and_stubs_stay_identical():
    t = _transcript(8)
    mgr = ContextManager(ContextPolicy(budget_tokens=3000, keep_recent_turns=3))
    first, _ = mgr.view(t)
    boundary = mgr.elide_before
    for i in range(8, 14):
        t += _turn(i, output_chars=4000)
    later, c = mgr.view(t)
    assert c is not None and mgr.elide_before > boundary
    # messages stubbed in the first jump are unchanged by the second
    assert later[:boundary] == first[:boundary]


def test_small_tool_results_are_not_worth_stubbing():
    t = _transcript(8, output_chars=50)
    t[3]["content"] = "y" * 5000  # one big one among small ones
    mgr = ContextManager(ContextPolicy(budget_tokens=500, keep_recent_turns=2))
    view, c = mgr.view(t)
    assert c is not None and c.elided == 1
    assert _stubbed(view[3])
    assert sum(_stubbed(m) for m in view) == 1
