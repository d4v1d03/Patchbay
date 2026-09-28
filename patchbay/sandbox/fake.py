from __future__ import annotations

import difflib
import io
import posixpath
import zipfile
from dataclasses import dataclass, field

from patchbay.sandbox.base import (
    WORKSPACE,
    ExecResult,
    SandboxFileNotFound,
    SandboxNotFound,
    resolve_path,
)


@dataclass
class ExecCall:
    command: str | list[str]
    timeout_s: int | None
    workdir: str
    env: dict[str, str] | None


@dataclass
class FakeSandbox:
    session_id: str = "fake"
    files: dict[str, str] = field(default_factory=dict)
    # substring of command -> result (str means exit 0 with that output)
    responses: dict[str, ExecResult | str] = field(default_factory=dict)
    default_response: ExecResult = field(
        default_factory=lambda: ExecResult(exit_code=0, output="", duration_ms=1)
    )
    calls: list[ExecCall] = field(default_factory=list)
    alive: bool = True
    _initial_files: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.files = {resolve_path(p): c for p, c in self.files.items()}
        self._initial_files = dict(self.files)

    # ------------------------------------------------------------------ commands

    def exec(
        self,
        command: str | list[str],
        *,
        timeout_s: int | None = None,
        workdir: str = WORKSPACE,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self._check_alive()
        self.calls.append(ExecCall(command, timeout_s, workdir, env))
        text = command if isinstance(command, str) else " ".join(command)
        for needle, response in self.responses.items():
            if needle in text:
                if isinstance(response, str):
                    return ExecResult(exit_code=0, output=response, duration_ms=1)
                return response
        return self.default_response

    # ------------------------------------------------------------------ files

    def read_file(self, path: str) -> str:
        self._check_alive()
        full = resolve_path(path)
        if full not in self.files:
            raise SandboxFileNotFound(f"no such file: {full}")
        return self.files[full]

    def write_file(self, path: str, content: str) -> None:
        self._check_alive()
        self.files[resolve_path(path)] = content

    def list_files(self) -> list[str]:
        return sorted(self.files)

    def diff(self) -> str:
        out: list[str] = []
        for path in sorted(set(self._initial_files) | set(self.files)):
            before = self._initial_files.get(path)
            after = self.files.get(path)
            if before == after:
                continue
            rel = posixpath.relpath(path, WORKSPACE)
            out.append(f"diff --git a/{rel} b/{rel}")
            out.extend(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=f"a/{rel}" if before is not None else "/dev/null",
                    tofile=f"b/{rel}" if after is not None else "/dev/null",
                )
            )
        return "".join(line if line.endswith("\n") else line + "\n" for line in out)

    def diff_stat(self) -> dict[str, int]:
        files = ins = dels = 0
        for path in set(self._initial_files) | set(self.files):
            before, after = self._initial_files.get(path), self.files.get(path)
            if before == after:
                continue
            files += 1
            b = set(enumerate((before or "").splitlines()))
            a = set(enumerate((after or "").splitlines()))
            ins += len(a - b)
            dels += len(b - a)
        return {"files": files, "insertions": ins, "deletions": dels}

    def export_zip(self, folder: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for path, content in sorted(self.files.items()):
                rel = posixpath.relpath(path, WORKSPACE)
                if not rel.startswith(("node_modules/", ".git/")):
                    zf.writestr(f"{folder}/{rel}", content)
        return buf.getvalue()

    # ------------------------------------------------------------------ lifecycle

    def is_alive(self) -> bool:
        return self.alive

    def destroy(self) -> None:
        self.alive = False

    def _check_alive(self) -> None:
        if not self.alive:
            raise SandboxNotFound("fake sandbox was destroyed")

    # ------------------------------------------------------------------ test helpers

    @property
    def commands(self) -> list[str]:
        return [
            c.command if isinstance(c.command, str) else " ".join(c.command) for c in self.calls
        ]
