"""One git worktree per task, outside the repository.

A task that writes code runs in its own worktree on its own branch. The user's
checkout is never touched while the attempts run, a failed task leaves nothing
behind, and a task that passed leaves exactly one branch to merge.

Between attempts the worktree is reset only when the *approach* was wrong (the
next attempt is a different model starting over). When the work was shallow,
the next, more thorough attempt continues from what the last one left, because
that is the work it was asked to finish.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class IsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    repo_dir: Path
    path: Path
    branch: str
    base: str


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise IsolationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def prepare(repo_dir: Path, task_id: int, root: Path) -> Workspace:
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    base = _git(top, "rev-parse", "HEAD")
    branch = f"cauce/task-{task_id}"
    path = root / "worktrees" / f"task-{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(top, "worktree", "add", "-b", branch, str(path), base)
    return Workspace(top, path, branch, base)


def reset(ws: Workspace) -> None:
    """Back to the base commit, in the task's worktree only."""
    _git(ws.path, "reset", "--hard", ws.base)
    _git(ws.path, "clean", "-fdq")


def has_changes(ws: Workspace) -> bool:
    status = _git(ws.path, "status", "--porcelain")
    ahead = _git(ws.path, "rev-list", "--count", f"{ws.base}..HEAD")
    return bool(status) or ahead not in ("", "0")


def finish(ws: Workspace, *, keep: bool, message: str) -> str | None:
    """Commit and keep the branch when `keep`, else drop both. Returns the branch kept."""
    kept = None
    if keep and has_changes(ws):
        if _git(ws.path, "status", "--porcelain"):
            _git(ws.path, "add", "-A")
            identity = [] if _git(ws.path, "config", "user.email", check=False) else [
                "-c", "user.name=cauce", "-c", "user.email=cauce@localhost"]
            _git(ws.path, *identity, "commit", "-q", "-m", message)
        kept = ws.branch
    _git(ws.repo_dir, "worktree", "remove", "--force", str(ws.path), check=False)
    if kept is None:
        _git(ws.repo_dir, "branch", "-D", ws.branch, check=False)
    return kept
