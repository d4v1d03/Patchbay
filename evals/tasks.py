from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from patchbay.sandbox import SandboxProtocol

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


@dataclass
class Task:
    name: str
    prompt: str
    kind: str = "empty"  # empty | repo
    repo_url: str | None = None
    setup: dict[str, str] = field(default_factory=dict)  # files written before the run
    hidden: dict[str, str] = field(default_factory=dict)  # files written after the run
    check: str | None = None  # command; exit 0 = pass
    expect: str | None = None  # "ask_user" → pass if the agent asked
    max_steps: int | None = None
    timeout_s: int = 900

    @classmethod
    def from_yaml(cls, path: Path) -> Task:
        data = yaml.safe_load(path.read_text())
        return cls(**data)

    def seed(self, sandbox: SandboxProtocol) -> None:
        for path, content in self.setup.items():
            sandbox.write_file(path, content)
        if self.setup:  # committed, so the diff shows only what the agent changed
            sandbox.exec(
                "git add -A && git -c user.name=eval -c user.email=eval@localhost "
                "commit -qm setup && git update-ref refs/patchbay/base HEAD",
                timeout_s=30,
            )

    def passed(self, status: str, check_exit: int | None) -> bool:
        if self.expect == "ask_user":
            return status == "waiting_for_user"
        return status == "completed" and check_exit == 0


def load_tasks(names: list[str] | None = None) -> list[Task]:
    tasks = [Task.from_yaml(p) for p in sorted(TASKS_DIR.glob("*.yaml"))]
    if names:
        by_name = {t.name: t for t in tasks}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SystemExit(f"unknown task(s): {', '.join(missing)}; have {', '.join(by_name)}")
        tasks = [by_name[n] for n in names]
    return tasks
