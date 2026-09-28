from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

WORKSPACE = "/workspace"


class SandboxError(Exception):
    pass


class SandboxNotFound(SandboxError):
    pass


class SandboxFileNotFound(SandboxError):
    pass


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def parse_numstat(output: str) -> dict[str, int]:
    """`git diff --numstat` → {files, insertions, deletions}. Binary files show as `-\t-`."""
    files = ins = dels = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        if parts[0].isdigit():
            ins += int(parts[0])
        if parts[1].isdigit():
            dels += int(parts[1])
    return {"files": files, "insertions": ins, "deletions": dels}


def resolve_path(path: str) -> str:
    if not path:
        return WORKSPACE
    if not path.startswith("/"):
        path = posixpath.join(WORKSPACE, path)
    return posixpath.normpath(path)


@runtime_checkable
class SandboxProtocol(Protocol):
    session_id: str

    def exec(
        self,
        command: str | list[str],
        *,
        timeout_s: int | None = None,
        workdir: str = WORKSPACE,
        env: dict[str, str] | None = None,
    ) -> ExecResult: ...

    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str) -> None: ...

    def diff(self) -> str: ...

    def diff_stat(self) -> dict[str, int]: ...

    def export_zip(self, folder: str) -> bytes: ...

    def is_alive(self) -> bool: ...

    def destroy(self) -> None: ...
