import secrets

import pytest

from patchbay.sandbox import Sandbox, SandboxFileNotFound, SandboxNotFound
from patchbay.sandbox.manager import LABEL_SESSION, docker_client

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox():
    sb = Sandbox.create(f"test-{secrets.token_hex(3)}")
    try:
        yield sb
    finally:
        sb.destroy()


def test_exec_runs_as_agent_in_workspace(sandbox):
    r = sandbox.exec("echo hello && whoami && pwd && id -u")
    assert r.ok
    assert r.output.splitlines() == ["hello", "agent", "/workspace", "1000"]
    assert r.duration_ms > 0 and not r.timed_out and not r.truncated


def test_exec_nonzero_exit_and_stderr_merged(sandbox):
    r = sandbox.exec("echo out; echo err >&2; exit 3")
    assert r.exit_code == 3
    assert "out" in r.output and "err" in r.output


def test_exec_argv_form_skips_shell(sandbox):
    r = sandbox.exec(["echo", "$HOME", "a b"])  # no shell → $HOME not expanded
    assert r.output == "$HOME a b\n"


def test_exec_timeout_kills_command(sandbox):
    r = sandbox.exec("sleep 30", timeout_s=2)
    assert r.timed_out
    assert r.exit_code == 124
    assert 1500 <= r.duration_ms < 10_000
    assert "timed out" in r.output


def test_exec_output_cap(sandbox):
    r = sandbox.exec("yes | head -c 300000", max_output_bytes=10_000)
    assert r.truncated
    assert len(r.output) < 10_200
    assert "truncated" in r.output


def test_capabilities_dropped_and_no_new_privs(sandbox):
    r = sandbox.exec("grep -E 'CapEff|NoNewPrivs' /proc/self/status")
    assert "CapEff:\t0000000000000000" in r.output
    assert "NoNewPrivs:\t1" in r.output


def test_write_read_roundtrip_with_nested_dirs_and_unicode(sandbox):
    content = "line one\nünïcödé ✓\n\ttabs and 'quotes' and $vars and `backticks`\n"
    sandbox.write_file("src/pkg/module.py", content)
    assert sandbox.read_file("src/pkg/module.py") == content
    assert sandbox.read_file("/workspace/src/pkg/module.py") == content
    owner = sandbox.exec(["stat", "-c", "%U:%G %a", "src/pkg/module.py"])
    assert owner.output.strip() == "agent:agent 644"
    with pytest.raises(SandboxFileNotFound):
        sandbox.read_file("does/not/exist.txt")


def test_diff_is_everything_since_the_session_started(sandbox):
    # New, uncommitted file: shows as an addition (thanks to `git add -N`).
    sandbox.write_file("hello.py", "print('hi')\n")
    d = sandbox.diff()
    assert "new file" in d and "+print('hi')" in d
    # The agent commits and edits again: still one new file since the start.
    assert sandbox.exec("git add -A && git commit -qm wip").ok
    sandbox.write_file("hello.py", "print('bye')\n")
    d2 = sandbox.diff()
    assert "new file" in d2 and "+print('bye')" in d2 and "hi" not in d2
    assert sandbox.diff_stat() == {"files": 1, "insertions": 1, "deletions": 0}
    sandbox.write_file("__pycache__/x.pyc", "junk")  # excluded from stats too
    assert sandbox.diff_stat()["files"] == 1


def test_diff_of_a_repository_is_against_its_starting_commit(sandbox):
    sandbox.write_file("hello.py", "print('hi')\n")
    assert sandbox.exec("git add -A && git commit -qm base").ok
    assert sandbox.exec("git update-ref refs/patchbay/base HEAD").ok  # as a clone starts
    sandbox.write_file("hello.py", "print('bye')\n")
    assert sandbox.exec("git commit -qam 'agent commit'").ok
    d = sandbox.diff()
    assert "-print('hi')" in d and "+print('bye')" in d and "new file" not in d
    assert sandbox.diff_stat() == {"files": 1, "insertions": 1, "deletions": 1}


def test_diff_follows_gitignore_and_leaves_the_agents_index_alone(sandbox):
    sandbox.write_file("app.py", "x = 1\n")
    sandbox.write_file("dist/bundle.js", "built")
    assert "dist/bundle.js" in sandbox.diff()  # not ignored yet
    sandbox.write_file(".gitignore", "dist\n")
    d = sandbox.diff()
    assert "dist/bundle.js" not in d and "app.py" in d and ".gitignore" in d
    assert sandbox.exec("git status --short").output.strip().splitlines() == [
        "?? .gitignore",
        "?? app.py",
    ]


def test_export_zip_is_the_project_as_git_would_keep_it(sandbox):
    import io
    import zipfile

    sandbox.write_file("src/app.py", "x = 1\n")
    sandbox.write_file(".gitignore", "dist\n")
    sandbox.write_file("dist/bundle.js", "built")
    sandbox.write_file("node_modules/lib/index.js", "lib")
    assert sandbox.exec("git add -A && git commit -qm wip").ok  # committed work is included
    sandbox.write_file("README.md", "# hi\n")
    zf = zipfile.ZipFile(io.BytesIO(sandbox.export_zip("proj")))
    assert sorted(zf.namelist()) == [
        "proj/",
        "proj/.gitignore",
        "proj/README.md",
        "proj/src/",
        "proj/src/app.py",
    ]
    assert zf.read("proj/src/app.py") == b"x = 1\n"


def test_attach_and_destroy_lifecycle():
    sb = Sandbox.create(f"test-{secrets.token_hex(3)}")
    cid = sb.container_id
    try:
        again = Sandbox.attach(cid)
        assert again.session_id == sb.session_id
        assert again.exec("echo ok").output == "ok\n"
        assert sb.is_alive()
    finally:
        sb.destroy()
    assert not sb.is_alive()
    with pytest.raises(SandboxNotFound):
        Sandbox.attach(cid)


def test_create_replaces_stale_container_with_same_name():
    sid = f"test-{secrets.token_hex(3)}"
    first = Sandbox.create(sid)
    second = Sandbox.create(sid)  # must not raise "name already in use"
    try:
        assert first.container_id != second.container_id
        assert not first.is_alive() and second.is_alive()
    finally:
        second.destroy()


def test_clone_public_repo():
    sid = f"test-{secrets.token_hex(3)}"
    try:
        sb = Sandbox.create(sid, repo_url="https://github.com/octocat/Hello-World")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clone failed, probably no network from the sandbox: {e}")
    try:
        r = sb.exec("git log --oneline -1 && ls")
        assert r.ok and "README" in r.output
    finally:
        sb.destroy()


def test_bad_repo_url_rejected_before_container_starts():
    with pytest.raises(ValueError):
        Sandbox.create("test-bad", repo_url="git@github.com:foo/bar.git")
    assert not docker_client().containers.list(all=True, filters={"name": "patchbay-test-bad"})


def test_no_leftover_containers():
    """Runs last (alphabetical order not guaranteed, but every fixture cleans
    up); a leak in any test above shows up here."""
    stray = docker_client().containers.list(all=True, filters={"label": f"{LABEL_SESSION}"})
    stray = [c for c in stray if c.name.startswith("patchbay-test-")]
    assert stray == [], [c.name for c in stray]


def test_reap_sandboxes_by_age_and_dead_state():
    from datetime import UTC, datetime, timedelta

    from patchbay.sandbox import reap_sandboxes

    young = Sandbox.create(f"test-{secrets.token_hex(3)}")
    old = Sandbox.create(f"test-{secrets.token_hex(3)}")
    dead = Sandbox.create(f"test-{secrets.token_hex(3)}")
    try:
        docker_client().containers.get(dead.container_id).stop(timeout=1)  # exited, not removed
        # only ours: a clock two hours ahead would reap every real session's sandbox too
        ours = {young.session_id, old.session_id, dead.session_id}
        reaped = reap_sandboxes(60, sessions=ours)  # real clock: only the dead one qualifies
        assert reaped == [dead.session_id]
        reaped = reap_sandboxes(60, now=datetime.now(UTC) + timedelta(hours=2), sessions=ours)
        assert set(reaped) == {young.session_id, old.session_id}
    finally:
        for sb in (young, old, dead):
            sb.destroy()
