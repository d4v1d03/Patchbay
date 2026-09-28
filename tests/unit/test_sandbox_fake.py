import io
import tarfile

import pytest

from patchbay.sandbox import (
    ExecResult,
    FakeSandbox,
    SandboxFileNotFound,
    SandboxNotFound,
    SandboxProtocol,
    resolve_path,
)
from patchbay.sandbox.manager import AGENT_UID, make_tar


def test_fake_satisfies_protocol():
    assert isinstance(FakeSandbox(), SandboxProtocol)


def test_resolve_path_relative_absolute_and_dotdot():
    assert resolve_path("app.py") == "/workspace/app.py"
    assert resolve_path("/etc/hosts") == "/etc/hosts"
    assert resolve_path("src/../app.py") == "/workspace/app.py"
    assert resolve_path("") == "/workspace"
    assert resolve_path("../outside") == "/outside"  # normalised; policy is the tool's job


def test_files_roundtrip_and_missing():
    sb = FakeSandbox(files={"README.md": "hi\n"})
    assert sb.read_file("README.md") == "hi\n"
    assert sb.read_file("/workspace/README.md") == "hi\n"
    sb.write_file("src/app.py", "print(1)\n")
    assert sb.read_file("src/app.py") == "print(1)\n"
    with pytest.raises(SandboxFileNotFound):
        sb.read_file("nope.txt")


def test_scripted_exec_and_call_recording():
    sb = FakeSandbox(
        responses={
            "pytest": ExecResult(exit_code=1, output="1 failed", duration_ms=5),
            "ls": "app.py\n",
        }
    )
    assert sb.exec("ls -la").output == "app.py\n"
    r = sb.exec("python -m pytest -q", timeout_s=30)
    assert r.exit_code == 1 and not r.ok
    assert sb.exec("echo nothing scripted").ok
    assert sb.commands == ["ls -la", "python -m pytest -q", "echo nothing scripted"]
    assert sb.calls[1].timeout_s == 30


def test_diff_shows_added_modified_and_deleted():
    sb = FakeSandbox(files={"a.txt": "one\n", "b.txt": "keep\n"})
    sb.write_file("a.txt", "one\ntwo\n")
    sb.write_file("c.txt", "new\n")
    d = sb.diff()
    assert "diff --git a/a.txt b/a.txt" in d and "+two" in d
    assert "diff --git a/c.txt b/c.txt" in d and "+new" in d
    assert "b.txt" not in d


def test_destroyed_sandbox_refuses_work():
    sb = FakeSandbox()
    sb.destroy()
    assert not sb.is_alive()
    with pytest.raises(SandboxNotFound):
        sb.exec("ls")


def test_make_tar_single_member_owned_by_agent():
    data = "héllo wörld\n".encode()
    with tarfile.open(fileobj=io.BytesIO(make_tar("hello.txt", data))) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == ["hello.txt"]
        m = members[0]
        assert m.isfile() and m.size == len(data)
        assert m.uid == AGENT_UID and m.gid == AGENT_UID
        assert m.mode == 0o644
        assert tar.extractfile(m).read() == data


def test_parse_numstat_counts_and_skips_binary():
    from patchbay.sandbox import parse_numstat

    out = "12\t3\tsrc/app.py\n-\t-\tlogo.png\n0\t7\told.txt\n"
    assert parse_numstat(out) == {"files": 3, "insertions": 12, "deletions": 10}
    assert parse_numstat("") == {"files": 0, "insertions": 0, "deletions": 0}


def test_fake_diff_stat():
    sb = FakeSandbox(files={"a.txt": "one\ntwo\n"})
    assert sb.diff_stat() == {"files": 0, "insertions": 0, "deletions": 0}
    sb.write_file("a.txt", "one\nthree\n")
    sb.write_file("b.txt", "new\n")
    assert sb.diff_stat() == {"files": 2, "insertions": 2, "deletions": 1}
