from patchbay.agent.tools import (
    BashTool,
    ReadFileTool,
    ToolRegistry,
    default_registry,
    truncate_middle,
)
from patchbay.llm.types import ToolCall
from patchbay.sandbox import ExecResult, FakeSandbox


def call(name, **args):
    return ToolCall(id="c1", name=name, arguments=args, raw_arguments="{}" if not args else "")


# ---------------------------------------------------------------- truncation


def test_truncate_middle_keeps_head_and_tail():
    text = "".join(f"line {i}\n" for i in range(1000))
    out, cut = truncate_middle(text, 500)
    assert cut and len(out) < 600
    assert out.startswith("line 0\n") and out.rstrip().endswith("line 999")
    assert "characters omitted" in out
    assert truncate_middle("short", 500) == ("short", False)


# ---------------------------------------------------------------- bash


def test_bash_appends_exit_code_and_clamps_timeout(monkeypatch):
    sb = FakeSandbox(
        responses={"pytest": ExecResult(exit_code=1, output="1 failed", duration_ms=30)}
    )
    r = BashTool().run(sb, command="python -m pytest -q", timeout_s=99_999)
    assert r.exit_code == 1 and r.is_error
    assert "1 failed" in r.output and "[exit code: 1]" in r.output
    assert sb.calls[0].timeout_s == 600  # clamped to MAX_TOOL_TIMEOUT_S
    r2 = BashTool().run(sb, command="true")
    assert r2.output.startswith("(no output)") and not r2.is_error
    assert sb.calls[1].timeout_s == 120  # default


# ---------------------------------------------------------------- read_file


def test_read_file_numbers_lines_and_windows():
    sb = FakeSandbox(files={"a.py": "one\ntwo\nthree\nfour\n"})
    r = ReadFileTool().run(sb, path="a.py")
    assert "(lines 1-4 of 4)" in r.output
    assert "1| one" in r.output and "4| four" in r.output
    r = ReadFileTool().run(sb, path="a.py", start_line=2, end_line=3)
    assert "2| two\n3| three" in r.output and "one" not in r.output.split("\n", 1)[1]
    assert "start_line=4" in r.output  # hint that more follows


def test_read_file_caps_window_and_reports_missing():
    sb = FakeSandbox(files={"big.txt": "\n".join(str(i) for i in range(1000))})
    r = ReadFileTool().run(sb, path="big.txt")
    assert "(lines 1-400 of 1000)" in r.output and "start_line=401" in r.output
    r = ReadFileTool().run(sb, path="nope.py")
    assert r.is_error and "list_files" in r.output


# ---------------------------------------------------------------- write_file


def test_write_file_creates_and_refuses_outside_workspace():
    sb = FakeSandbox()
    reg = default_registry()
    r = reg.run(call("write_file", path="src/new.py", content="x = 1\n"), sb)
    assert not r.is_error and sb.read_file("src/new.py") == "x = 1\n"
    r = reg.run(call("write_file", path="../../etc/passwd", content="bad"), sb)
    assert r.is_error and "outside" in r.output
    assert "/etc/passwd" not in sb.files


# ---------------------------------------------------------------- list_files / search


def test_list_files_builds_pruned_find_and_search_handles_no_match():
    sb = FakeSandbox(
        responses={
            "find": "./a.py\n./src\n",
            "rg": ExecResult(exit_code=1, output="", duration_ms=1),
        }
    )
    reg = default_registry()
    r = reg.run(call("list_files", depth=9), sb)
    assert "./a.py" in r.output
    cmd = sb.commands[0]
    assert "-maxdepth 5" in cmd and "node_modules" in cmd and cmd.startswith("cd /workspace")
    r = reg.run(call("search", pattern="def main", glob="*.py"), sb)
    assert r.output == "no matches" and not r.is_error
    assert sb.calls[1].command[:2] == ["rg", "-n"] and "-g" in sb.calls[1].command


# ---------------------------------------------------------------- terminal tools


def test_finish_and_ask_user_are_terminal():
    reg = default_registry()
    assert reg.is_terminal("finish") and reg.is_terminal("ask_user")
    assert not reg.is_terminal("bash") and not reg.is_terminal("nope")
    r = reg.run(call("finish", summary="all done"), FakeSandbox())
    assert r.output == "all done"
    r = reg.run(call("ask_user", question="which db?"), FakeSandbox())
    assert "which db?" in r.output


# ---------------------------------------------------------------- registry guarantees


def test_registry_schemas_are_openai_shaped():
    schemas = default_registry().schemas()
    names = [s["function"]["name"] for s in schemas]
    assert names == [
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "search",
        "ask_user",
        "finish",
    ]
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"
        assert s["function"]["description"]


def test_registry_turns_problems_into_error_results_never_exceptions():
    sb = FakeSandbox()
    reg = default_registry()
    assert reg.run(call("nope"), sb).is_error
    r = reg.run(ToolCall(id="c", name="bash", arguments={}, raw_arguments='{"command": '), sb)
    assert r.is_error and "not valid JSON" in r.output
    r = reg.run(call("bash"), sb)  # missing required `command`
    assert r.is_error and "missing required" in r.output
    r = reg.run(call("bash", command="ls", bogus=1), sb)  # unexpected kwarg → TypeError
    assert r.is_error and "bad arguments" in r.output


def test_registry_bounds_every_output():
    sb = FakeSandbox(responses={"cat": "x" * 50_000})
    reg = ToolRegistry([BashTool()], max_output_chars=1000)
    r = reg.run(call("bash", command="cat big"), sb)
    assert r.truncated and len(r.output) < 1200 and "omitted" in r.output


# ---------------------------------------------------------------- edit_file


class TestEditFile:
    def test_replaces_unique_match(self):
        sb = FakeSandbox(files={"app.py": "def main():\n    return 1\n"})
        r = default_registry().run(
            call("edit_file", path="app.py", old="return 1", new="return 2"), sb
        )
        assert not r.is_error and "app.py" in r.output
        assert sb.read_file("app.py") == "def main():\n    return 2\n"

    def test_not_found_says_so_helpfully(self):
        sb = FakeSandbox(files={"app.py": "x = 1\n"})
        r = default_registry().run(call("edit_file", path="app.py", old="y = 2", new="y = 3"), sb)
        assert (
            r.is_error and "not found" in r.output.lower() and sb.read_file("app.py") == "x = 1\n"
        )

    def test_ambiguous_match_reports_count(self):
        sb = FakeSandbox(files={"app.py": "a\nb\na\n"})
        r = default_registry().run(call("edit_file", path="app.py", old="a", new="c"), sb)
        assert r.is_error and "2" in r.output and sb.read_file("app.py") == "a\nb\na\n"

    def test_missing_file_and_outside_workspace(self):
        sb = FakeSandbox()
        reg = default_registry()
        assert reg.run(call("edit_file", path="nope.py", old="a", new="b"), sb).is_error
        assert reg.run(call("edit_file", path="/etc/hosts", old="a", new="b"), sb).is_error
