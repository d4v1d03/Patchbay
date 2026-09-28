"""Tool descriptions are prompts: written for the model, not for someone reading the code."""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any

from patchbay.config import get_settings
from patchbay.llm.types import ToolCall
from patchbay.sandbox import (
    WORKSPACE,
    SandboxError,
    SandboxFileNotFound,
    SandboxNotFound,
    SandboxProtocol,
    resolve_path,
)

log = logging.getLogger(__name__)

MAX_READ_LINES = 400


@dataclass
class ToolResult:
    output: str
    exit_code: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    is_error: bool = False

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(output=f"Error: {message}", is_error=True)


class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments
    terminal: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, sandbox: SandboxProtocol, **kwargs: Any) -> ToolResult:
        raise NotImplementedError


def truncate_middle(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n... [{omitted} characters omitted] ...\n\n{text[-tail:]}", True


def _is_json_object(raw: str) -> bool:
    if not raw.strip():
        return True
    try:
        return isinstance(json.loads(raw), dict)
    except json.JSONDecodeError:
        return False


def _workspace_path(path: str) -> str:
    full = resolve_path(path)
    if full != WORKSPACE and not full.startswith(WORKSPACE + "/"):
        raise ValueError(f"path {full} is outside {WORKSPACE}; only the workspace is writable")
    return full


# ---------------------------------------------------------------------- tools


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command in the sandbox (bash, in /workspace) and return its combined "
        "stdout+stderr and exit code. Use for running tests, installing packages, git, and "
        "anything else a developer would type in a terminal. Long output is truncated: prefer "
        "`| head -50`, `-q`, `--quiet`. Commands have no interactive input; do not run editors "
        "or programs that wait for a keypress."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to run, e.g. `python -m pytest -q`",
            },
            "timeout_s": {
                "type": "integer",
                "description": "Seconds before the command is killed (default 120, max 600).",
            },
        },
        "required": ["command"],
    }

    def run(
        self, sandbox: SandboxProtocol, command: str, timeout_s: int | None = None
    ) -> ToolResult:
        s = get_settings()
        timeout = min(int(timeout_s or s.default_tool_timeout_s), s.max_tool_timeout_s)
        r = sandbox.exec(command, timeout_s=timeout)
        out = r.output if r.output.strip() else "(no output)"
        out += f"\n[exit code: {r.exit_code}]"
        return ToolResult(out, exit_code=r.exit_code, duration_ms=r.duration_ms, is_error=not r.ok)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file and return its contents with line numbers. For large files pass "
        f"start_line/end_line to read a window (at most {MAX_READ_LINES} lines per call)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, relative to /workspace or absolute.",
            },
            "start_line": {"type": "integer", "description": "First line to return (1-based)."},
            "end_line": {"type": "integer", "description": "Last line to return (inclusive)."},
        },
        "required": ["path"],
    }

    def run(
        self,
        sandbox: SandboxProtocol,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        try:
            text = sandbox.read_file(path)
        except SandboxFileNotFound:
            return ToolResult.error(f"no such file: {path}. Use list_files to see what exists.")
        lines = text.splitlines()
        total = len(lines)
        start = max(1, start_line or 1)
        end = min(total, end_line or total)
        if end - start + 1 > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1
        window = lines[start - 1 : end]
        width = len(str(end)) if end else 1
        body = "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(window, start=start))
        if not lines:
            body = "(empty file)"
        header = f"{resolve_path(path)} (lines {start}-{end} of {total})"
        if end < total:
            header += f" — call again with start_line={end + 1} for more"
        return ToolResult(f"{header}\n{body}")


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one with the given content. "
        "Parent directories are created. For small changes to an existing file prefer edit_file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to /workspace."},
            "content": {"type": "string", "description": "The full new contents of the file."},
        },
        "required": ["path", "content"],
    }

    def run(self, sandbox: SandboxProtocol, path: str, content: str) -> ToolResult:
        try:
            full = _workspace_path(path)
        except ValueError as e:
            return ToolResult.error(str(e))
        sandbox.write_file(full, content)
        return ToolResult(f"wrote {len(content.encode())} bytes to {full}")


class EditFileTool(Tool):
    """Replace one exact, unique occurrence of `old` with `new`. Error messages
    are written for the model: they say what to do next."""

    name = "edit_file"
    description = (
        "Edit a file by replacing one exact occurrence of `old` with `new`. `old` must match "
        "the file text exactly (whitespace included) and must be unique in the file — include "
        "enough surrounding lines to make it unique. Read the file first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path, relative to /workspace."},
            "old": {"type": "string", "description": "Exact existing text to replace."},
            "new": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old", "new"],
    }

    def run(self, sandbox: SandboxProtocol, path: str, old: str, new: str) -> ToolResult:
        try:
            full = _workspace_path(path)
        except ValueError as e:
            return ToolResult.error(str(e))

        try:
            text = sandbox.read_file(full)
        except SandboxFileNotFound:
            return ToolResult.error(f"no such file: {path}. Use list_files to see what exists.")

        count = text.count(old)
        if count == 0:
            return ToolResult.error(
                f"`old` text not found in {full}. Read the file and copy the exact text "
                "to replace, including whitespace and indentation."
            )
        if count > 1:
            return ToolResult.error(
                f"`old` text occurs {count} times in {full}; include more surrounding lines "
                "so it matches exactly once."
            )
        new_text = text.replace(old, new, 1)
        sandbox.write_file(full, new_text)
        removed = old.count("\n") + 1
        added = new.count("\n") + 1
        return ToolResult(f"edited {full} (-{removed}/+{added} lines)")


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files and directories under a path as a tree (default: /workspace, depth 2). "
        "Skips .git, node_modules, .venv and __pycache__. Use it first to orient yourself."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: /workspace)."},
            "depth": {"type": "integer", "description": "How many levels deep (default 2, max 5)."},
        },
        "required": [],
    }

    def run(self, sandbox: SandboxProtocol, path: str = ".", depth: int = 2) -> ToolResult:
        depth = max(1, min(int(depth or 2), 5))
        full = resolve_path(path)
        cmd = (
            f"cd {shlex.quote(full)} && find . -maxdepth {depth} "
            r"\( -name .git -o -name node_modules -o -name .venv -o -name __pycache__ \) -prune "
            "-o -print | sort | head -300"
        )
        r = sandbox.exec(cmd, timeout_s=30)
        if not r.ok:
            return ToolResult.error(f"could not list {full}: {r.output.strip()}")
        return ToolResult(f"{full}:\n{r.output}")


class SearchTool(Tool):
    name = "search"
    description = (
        "Search file contents with ripgrep (regex). Returns matching lines as path:line:text, "
        "at most 50 matches per file. Use to find where something is defined or used before "
        "reading whole files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "Directory or file to search (default: /workspace).",
            },
            "glob": {
                "type": "string",
                "description": "Only search files matching this glob, e.g. `*.py`.",
            },
        },
        "required": ["pattern"],
    }

    def run(
        self, sandbox: SandboxProtocol, pattern: str, path: str = ".", glob: str | None = None
    ) -> ToolResult:
        argv = [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            "50",
            "--max-columns",
            "200",
        ]
        if glob:
            argv += ["-g", glob]
        argv += ["-e", pattern, resolve_path(path)]
        r = sandbox.exec(argv, timeout_s=60)
        if r.exit_code == 1:  # ripgrep: 1 = no matches, 2 = error
            return ToolResult("no matches")
        if r.exit_code not in (0, None):
            return ToolResult.error(f"search failed: {r.output.strip()}")
        return ToolResult(r.output)


class AskUserTool(Tool):
    name = "ask_user"
    terminal = True
    description = (
        "Pause and ask the user a question when the task is genuinely ambiguous and the answer "
        "cannot be found in the code. The run stops until they reply. Do not use it for things "
        "you can check yourself."
    )
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "The question for the user."}},
        "required": ["question"],
    }

    def run(self, sandbox: SandboxProtocol, question: str) -> ToolResult:
        return ToolResult(f"Waiting for the user to answer: {question}")


class FinishTool(Tool):
    name = "finish"
    terminal = True
    description = (
        "Call this when the task is complete and verified. Summarise what you changed, which "
        "files, and how you verified it (e.g. which tests you ran and their result)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was done and how it was verified."}
        },
        "required": ["summary"],
    }

    def run(self, sandbox: SandboxProtocol, summary: str) -> ToolResult:
        return ToolResult(summary)


# ---------------------------------------------------------------------- registry


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, *, max_output_chars: int | None = None):
        self._tools: dict[str, Tool] = {}
        self.max_output_chars = max_output_chars or get_settings().max_tool_output_chars
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def is_terminal(self, name: str) -> bool:
        t = self._tools.get(name)
        return bool(t and t.terminal)

    def run(self, call: ToolCall, sandbox: SandboxProtocol) -> ToolResult:
        """Never raises for tool-level problems: they come back as an error result
        the model can react to. Only SandboxNotFound propagates, since a missing
        container is the worker's problem.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return self._bounded(
                ToolResult.error(f"unknown tool {call.name!r}. Available: {', '.join(self._tools)}")
            )
        if not call.arguments and not _is_json_object(call.raw_arguments):
            return self._bounded(
                ToolResult.error(
                    f"arguments for {call.name} were not valid JSON: {call.raw_arguments[:200]}"
                )
            )
        missing = [p for p in tool.parameters.get("required", []) if p not in call.arguments]
        if missing:
            return self._bounded(
                ToolResult.error(
                    f"{call.name} is missing required argument(s): {', '.join(missing)}"
                )
            )
        try:
            result = tool.run(sandbox, **call.arguments)
        except SandboxNotFound:
            raise
        except TypeError as e:  # unexpected/misspelled argument names
            result = ToolResult.error(f"bad arguments for {call.name}: {e}")
        except SandboxError as e:
            result = ToolResult.error(f"{call.name} failed: {e}")
        except NotImplementedError as e:
            result = ToolResult.error(str(e))
        except Exception as e:  # noqa: BLE001 — a tool bug must not kill the run
            log.exception("tool %s crashed", call.name)
            result = ToolResult.error(f"{call.name} crashed: {type(e).__name__}: {e}")
        return self._bounded(result)

    def _bounded(self, result: ToolResult) -> ToolResult:
        result.output, cut = truncate_middle(result.output, self.max_output_chars)
        result.truncated = result.truncated or cut
        return result


def default_registry(**kwargs: Any) -> ToolRegistry:
    return ToolRegistry(
        [
            BashTool(),
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            ListFilesTool(),
            SearchTool(),
            AskUserTool(),
            FinishTool(),
        ],
        **kwargs,
    )
