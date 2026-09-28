"""Prompts are versioned Markdown files in prompts/, compared on the eval suite
before a new version becomes the default.

From v2 the system prompt is static and the session details (repo, network,
workspace listing) go in the first user message. Providers cache by prefix and
the tool definitions are sent after the system prompt, so anything
session-specific there stops the tools being cached across sessions.
"""

from __future__ import annotations

from pathlib import Path

from patchbay.llm.types import Message, system_message, user_message
from patchbay.sandbox import SandboxProtocol

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_VERSION = "v0"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def workspace_tree(sandbox: SandboxProtocol, depth: int = 2, max_lines: int = 150) -> str:
    r = sandbox.exec(
        f"find . -maxdepth {depth} "
        r"\( -name .git -o -name node_modules -o -name .venv -o -name __pycache__ \) -prune "
        f"-o -print | sort | head -{max_lines}",
        timeout_s=30,
    )
    listing = r.output.strip() if r.ok else "(could not list workspace)"
    return listing or "(empty)"


def build_system_prompt(
    *,
    tree: str,
    repo_url: str | None = None,
    network: bool = True,
    version: str = SYSTEM_PROMPT_VERSION,
) -> str:
    template = load_prompt(f"system_{version}")
    return template.format(
        repo_line=f" (cloned from {repo_url})"
        if repo_url
        else " (empty; start a new project here)",
        network="yes" if network else "no",
        tree=tree,
    )


def build_initial_messages(
    *,
    task: str,
    tree: str,
    repo_url: str | None = None,
    network: bool = True,
    version: str = SYSTEM_PROMPT_VERSION,
) -> list[Message]:
    fields = {
        "repo_line": f" (cloned from {repo_url})"
        if repo_url
        else " (empty; start a new project here)",
        "network": "yes" if network else "no",
        "tree": tree,
        # descriptive, not imperative: in the user turn, "start a new project here"
        # read as an instruction and made the agent skip ask_user on vague tasks
        "repo": (
            f"cloned from {repo_url} into /workspace" if repo_url else "none; /workspace is empty"
        ),
    }
    session = PROMPTS_DIR / f"session_{version}.md"
    if session.exists():
        context = session.read_text(encoding="utf-8").format(**fields).rstrip()
        return [
            system_message(load_prompt(f"system_{version}").strip()),
            user_message(f"{context}\n\n## Task\n{task.strip()}"),
        ]
    return [
        system_message(load_prompt(f"system_{version}").format(**fields)),
        user_message(task.strip()),
    ]
