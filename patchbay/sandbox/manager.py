from __future__ import annotations

import io
import logging
import secrets
import shlex
import tarfile
import time
from datetime import UTC, datetime

import docker
import docker.errors
from docker.models.containers import Container

from patchbay.config import get_settings
from patchbay.sandbox.base import (
    WORKSPACE,
    ExecResult,
    SandboxError,
    SandboxFileNotFound,
    SandboxNotFound,
    parse_numstat,
    resolve_path,
)

log = logging.getLogger(__name__)

LABEL_SESSION = "patchbay.session"
LABEL_CREATED = "patchbay.created"
SANDBOX_USER = "agent"
AGENT_UID = 1000

# env for every exec: no prompts, no colour, no pagers
DEFAULT_EXEC_ENV = {
    "HOME": "/home/agent",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "PYTHONUNBUFFERED": "1",
    "PIP_PROGRESS_BAR": "off",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "DEBIAN_FRONTEND": "noninteractive",
}

MAX_OUTPUT_BYTES = 1_000_000
TIMEOUT_EXIT_CODE = 124
CLONE_TIMEOUT_S = 300

# Where the session started. Diffs are taken against it, not HEAD, so work the
# agent commits still shows. An empty workspace starts at git's empty tree.
BASE_REF = "refs/patchbay/base"
_BASE = (
    f"$(git rev-parse -q --verify {BASE_REF} || git rev-parse -q --verify HEAD "
    "|| git hash-object -t tree /dev/null)"
)

# git pathspec excludes for diff()
DIFF_EXCLUDES = [
    ":!*__pycache__*",
    ":!*.pyc",
    ":!*.pytest_cache*",
    ":!*.ruff_cache*",
    ":!*.mypy_cache*",
    ":!*node_modules*",
    ":!*.venv*",
    ":!*.egg-info*",
]

_client: docker.DockerClient | None = None


def docker_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


class Sandbox:
    def __init__(self, container: Container):
        self._container = container
        labels = container.labels or {}
        self.session_id: str = labels.get(LABEL_SESSION, "")
        self.container_id: str = container.id

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        session_id: str,
        *,
        repo_url: str | None = None,
        image: str | None = None,
        network: bool | None = None,
    ) -> Sandbox:
        settings = get_settings()
        image = image or settings.sandbox_image
        if network is None:
            network = settings.sandbox_network != "none"
        if repo_url is not None and not repo_url.startswith(("https://", "http://")):
            raise ValueError("repo_url must be an http(s) URL (no SSH keys in the sandbox)")

        client = docker_client()
        name = f"patchbay-{session_id}"
        _remove_if_exists(client, name)

        try:
            container = client.containers.run(
                image,
                command=["sleep", "infinity"],
                detach=True,
                name=name,
                hostname="sandbox",
                labels={
                    LABEL_SESSION: session_id,
                    LABEL_CREATED: datetime.now(UTC).isoformat(timespec="seconds"),
                },
                user=SANDBOX_USER,
                working_dir=WORKSPACE,
                mem_limit=settings.sandbox_memory,
                nano_cpus=int(settings.sandbox_cpus * 1e9),
                pids_limit=512,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                network_mode="bridge" if network else "none",
            )
        except docker.errors.ImageNotFound as e:
            raise SandboxError(
                f"sandbox image {image!r} not found — build it with `make build-sandbox`"
            ) from e
        except docker.errors.APIError as e:
            raise SandboxError(f"docker run failed: {e.explanation}") from e

        sandbox = cls(container)
        try:
            sandbox._init_workspace(repo_url)
        except Exception:
            sandbox.destroy()
            raise
        log.info(
            "sandbox %s created for session %s (repo=%s)", container.short_id, session_id, repo_url
        )
        return sandbox

    @classmethod
    def attach(cls, container_id: str) -> Sandbox:
        try:
            container = docker_client().containers.get(container_id)
        except docker.errors.NotFound as e:
            raise SandboxNotFound(f"container {container_id} no longer exists") from e
        if container.status != "running":
            raise SandboxNotFound(f"container {container_id} is {container.status}, not running")
        return cls(container)

    def _init_workspace(self, repo_url: str | None) -> None:
        if repo_url:
            result = self.exec(
                ["git", "clone", "--depth", "50", "--quiet", repo_url, "."],
                timeout_s=CLONE_TIMEOUT_S,
            )
            if not result.ok:
                raise SandboxError(
                    f"git clone failed (exit {result.exit_code}):\n{result.output[-2000:]}"
                )
            base = "HEAD"
        else:
            result = self.exec(["git", "init", "--quiet"], timeout_s=30)
            if not result.ok:
                raise SandboxError(f"git init failed:\n{result.output}")
            base = "$(git hash-object -t tree /dev/null)"
        self.exec(f"git update-ref {BASE_REF} {base}", timeout_s=30)

    def is_alive(self) -> bool:
        try:
            self._container.reload()
        except docker.errors.NotFound:
            return False
        return self._container.status == "running"

    def destroy(self) -> None:
        try:
            self._container.remove(force=True)
            log.info("sandbox %s destroyed", self.container_id[:12])
        except docker.errors.NotFound:
            pass

    # ------------------------------------------------------------------ commands

    def exec(
        self,
        command: str | list[str],
        *,
        timeout_s: int | None = None,
        workdir: str = WORKSPACE,
        env: dict[str, str] | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> ExecResult:
        timeout_s = timeout_s or get_settings().default_tool_timeout_s
        argv = ["bash", "-lc", command] if isinstance(command, str) else list(command)
        argv = ["timeout", "-k", "5", f"{timeout_s}s", *argv]

        api = docker_client().api
        started = time.monotonic()
        try:
            exec_id = api.exec_create(
                self._container.id,
                argv,
                stdout=True,
                stderr=True,
                workdir=workdir,
                user=SANDBOX_USER,
                environment={**DEFAULT_EXEC_ENV, **(env or {})},
            )["Id"]
            stream = api.exec_start(exec_id, stream=True, demux=False)

            # keep draining past the cap so the process runs to completion
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in stream:
                if truncated:
                    continue
                if size + len(chunk) > max_output_bytes:
                    chunks.append(chunk[: max_output_bytes - size])
                    truncated = True
                else:
                    chunks.append(chunk)
                size += len(chunk)

            exit_code = api.exec_inspect(exec_id)["ExitCode"]
        except docker.errors.NotFound as e:
            raise SandboxNotFound(f"container {self.container_id[:12]} is gone") from e
        except docker.errors.APIError as e:
            raise SandboxError(f"exec failed: {e.explanation}") from e

        duration_ms = int((time.monotonic() - started) * 1000)
        if exit_code is None:
            exit_code = -1
        output = b"".join(chunks).decode("utf-8", errors="replace")
        timed_out = exit_code == TIMEOUT_EXIT_CODE or (
            exit_code == 137 and duration_ms >= timeout_s * 1000
        )
        if truncated:
            output += f"\n[output truncated at {max_output_bytes} bytes]"
        if timed_out:
            output += f"\n[command timed out after {timeout_s}s]"
        return ExecResult(
            exit_code=exit_code,
            output=output,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=truncated,
        )

    # ------------------------------------------------------------------ files

    def read_file(self, path: str) -> str:
        return self._read_bytes(resolve_path(path)).decode("utf-8", errors="replace")

    def _read_bytes(self, full: str) -> bytes:
        try:
            bits, _stat = self._container.get_archive(full)
        except docker.errors.NotFound as e:
            raise SandboxFileNotFound(f"no such file: {full}") from e
        except docker.errors.APIError as e:
            raise SandboxError(f"read failed: {e.explanation}") from e

        buf = io.BytesIO(b"".join(bits))
        with tarfile.open(fileobj=buf) as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            if not members:
                raise SandboxFileNotFound(f"not a regular file: {full}")
            f = tar.extractfile(members[0])
            assert f is not None
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        full = resolve_path(path)
        parent, name = full.rsplit("/", 1)
        parent = parent or "/"
        mk = self.exec(["mkdir", "-p", parent], timeout_s=10)
        if not mk.ok:
            raise SandboxError(f"could not create directory {parent}: {mk.output}")
        try:
            self._container.put_archive(parent, make_tar(name, content.encode("utf-8")))
        except docker.errors.APIError as e:
            raise SandboxError(f"write_file failed: {e.explanation}") from e

    def diff(self) -> str:
        result = self.exec(_diff_command("--no-color"), timeout_s=60, max_output_bytes=5_000_000)
        return result.output

    def diff_stat(self) -> dict[str, int]:
        r = self.exec(_diff_command("--numstat"), timeout_s=30)
        return parse_numstat(r.output if r.ok else "")

    def export_zip(self, folder: str) -> bytes:
        out = f"{WORKSPACE}/.git/patchbay-export.zip"
        r = self.exec(
            f"{_stage_command()} && git archive --format=zip "
            f'--prefix={shlex.quote(folder)}/ -o {out} "$(git write-tree)"',
            timeout_s=120,
        )
        if not r.ok:
            raise SandboxError(f"export failed (exit {r.exit_code}): {r.output[-500:]}")
        try:
            return self._read_bytes(out)
        finally:
            self.exec(["rm", "-f", out], timeout_s=10)


# ---------------------------------------------------------------------- helpers


_PATHSPEC = " ".join(shlex.quote(p) for p in [".", *DIFF_EXCLUDES])


def _stage_command() -> str:
    """Stage the workspace as git would keep it (.gitignore as it stands now)
    in a private index, so the agent's own index is never touched. Each call
    resets it to the base; `read-tree --reset` keeps stat info, so unchanged
    files aren't re-hashed, and a file ignored after it first appeared drops out.
    """
    return (
        f"base={_BASE} && "
        'export GIT_INDEX_FILE="$(git rev-parse --absolute-git-dir)/patchbay-index" && '
        f"git read-tree --reset $base && git add -A -- {_PATHSPEC}"
    )


def _diff_command(option: str) -> str:
    return f"{_stage_command()} && git diff --cached {option} $base -- {_PATHSPEC}"


def make_tar(name: str, data: bytes, *, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = mode
        info.uid = info.gid = AGENT_UID
        info.uname = info.gname = SANDBOX_USER
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _remove_if_exists(client: docker.DockerClient, name: str) -> None:
    try:
        client.containers.get(name).remove(force=True)
        log.warning("removed stale container %s", name)
    except docker.errors.NotFound:
        pass


def list_sandboxes(*, all_states: bool = True) -> list[Container]:
    return docker_client().containers.list(all=all_states, filters={"label": LABEL_SESSION})


def reap_sandboxes(
    ttl_minutes: int, *, now: datetime | None = None, sessions: set[str] | None = None
) -> list[str]:
    """Remove sandboxes older than `ttl_minutes` (by their `patchbay.created`
    label) and any that are no longer running; only `sessions` if given.
    Returns their session ids."""
    now = now or datetime.now(UTC)
    reaped: list[str] = []
    for c in list_sandboxes():
        labels = c.labels or {}
        if sessions is not None and labels.get(LABEL_SESSION) not in sessions:
            continue
        created = labels.get(LABEL_CREATED)
        try:
            age_min = (
                (now - datetime.fromisoformat(created)).total_seconds() / 60 if created else None
            )
        except ValueError:
            age_min = None
        if c.status == "running" and age_min is not None and age_min < ttl_minutes:
            continue
        try:
            c.remove(force=True)
            reaped.append(labels.get(LABEL_SESSION, ""))
            log.info(
                "reaped sandbox %s (session %s, age %s min)",
                c.short_id,
                labels.get(LABEL_SESSION),
                age_min,
            )
        except docker.errors.APIError as e:
            log.warning("could not remove %s: %s", c.short_id, e.explanation)
    return reaped


# ---------------------------------------------------------------------- CLI demo


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m patchbay.sandbox",
        description="Create a sandbox, run one command in it, print the result.",
    )
    parser.add_argument("command", help="shell command to run in /workspace")
    parser.add_argument("--repo", help="public http(s) git URL to clone first")
    parser.add_argument("--image", help="override SANDBOX_IMAGE")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--diff", action="store_true", help="print git diff afterwards")
    parser.add_argument("--keep", action="store_true", help="don't destroy the container")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    session_id = f"demo-{secrets.token_hex(3)}"
    t0 = time.monotonic()
    sb = Sandbox.create(
        session_id,
        repo_url=args.repo,
        image=args.image,
        network=not args.no_network,
    )
    print(f"# sandbox {sb.container_id[:12]} ready in {time.monotonic() - t0:.1f}s")
    try:
        r = sb.exec(args.command, timeout_s=args.timeout)
        print(f"# $ {args.command}")
        print(r.output, end="" if r.output.endswith("\n") else "\n")
        print(
            f"# exit={r.exit_code} timed_out={r.timed_out} "
            f"truncated={r.truncated} {r.duration_ms}ms"
        )
        if args.diff:
            print("# --- git diff ---")
            print(sb.diff() or "(no changes)")
        return r.exit_code
    finally:
        if args.keep:
            print(f"# kept: docker exec -it {sb.container_id[:12]} bash")
        else:
            sb.destroy()


if __name__ == "__main__":
    raise SystemExit(_main())
