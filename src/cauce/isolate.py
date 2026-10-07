"""One git worktree per task, outside the repository.

A task that writes code runs in its own worktree on its own branch. The user's
checkout is never touched while the attempts run, and a task that passed leaves
exactly one branch to merge.

A task that stopped for a person — blocked on a refused command, a spent budget,
a cell that needs approval — keeps its branch too, marked unverified: the work
its workers did is real, and throwing it away is what made "I created the
routes" read as a lie when the files were nowhere. `cauce resume` continues on
that branch. A task that failed or must be replanned leaves nothing behind.

Between attempts the worktree is reset only when the *approach* was wrong (the
next attempt is a different model starting over). When the work was shallow,
the next, more thorough attempt continues from what the last one left, because
that is the work it was asked to finish.

A worktree has only what git carries. The checkout's ignored dependency folders
(`node_modules`, a virtualenv) are linked in and the person's own
`.claude/settings.local.json` is copied in, so a build can run and the worker
is allowed — and refused — what the person set; both are taken out again
before anything is committed.
"""
from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Folders a build needs and git does not carry. Linked, not copied: shared with
#: the checkout, which is why the brief tells the worker not to reinstall them.
DEPENDENCY_DIRS = frozenset({"node_modules", ".venv", "venv"})
#: How the commit of work kept without a pass is marked.
UNVERIFIED = "(unverified"
#: The person's own permission rules for the project, never committed. Copied,
#: not linked: the worker reads its own copy, and nothing it does reaches theirs.
LOCAL_SETTINGS = ".claude/settings.local.json"


class IsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    repo_dir: Path
    path: Path
    branch: str
    base: str
    #: Paths, relative to the checkout, linked (or, a file, copied) into the worktree.
    links: tuple[str, ...] = ()


def _git(cwd: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise IsolationError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip() if strip else proc.stdout


def branch_for(task_id: int) -> str:
    return f"cauce/task-{task_id}"


def kept(repo_dir: Path, task_id: int) -> bool:
    """Whether the task's branch exists: work it kept, verified or not."""
    return bool(_git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_for(task_id)}", check=False))


def prepare(repo_dir: Path, task_id: int, root: Path) -> Workspace:
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    head = _git(top, "rev-parse", "HEAD")
    branch = branch_for(task_id)
    path = root / "worktrees" / f"task-{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(top, "worktree", "prune", check=False)  # a run that died left its entry behind
    if kept(top, task_id):
        # A resumed task continues from the work it kept.
        _git(top, "worktree", "add", str(path), branch)
        base = _git(top, "merge-base", head, branch)
    else:
        _git(top, "worktree", "add", "-b", branch, str(path), head)
        base = head
    ws = Workspace(top, path, branch, base, _shared(top, path))
    link(ws)
    return ws


def _shared(top: Path, path: Path) -> tuple[str, ...]:
    ignored = _git(top, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
                   check=False, strip=False)
    found = [e.rstrip("/") for e in ignored.split("\0") if e.endswith("/") and Path(e.rstrip("/")).name
             in DEPENDENCY_DIRS]
    if (top / LOCAL_SETTINGS).is_file() and not (path / LOCAL_SETTINGS).exists():
        found.append(LOCAL_SETTINGS)
    return tuple(sorted(found))


def link(ws: Workspace) -> None:
    for rel in ws.links:
        place, target = ws.path / rel, ws.repo_dir / rel
        if place.exists() or place.is_symlink() or not target.exists():
            continue
        place.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            place.symlink_to(target, target_is_directory=True)
        else:
            shutil.copyfile(target, place)


def unlink(ws: Workspace) -> None:
    for rel in ws.links:
        place = ws.path / rel
        if place.is_symlink() or place.is_file():
            place.unlink()
        parent = place.parent
        while parent != ws.path and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def _linked(ws: Workspace, path: str) -> bool:
    return any(path == rel or path.startswith(rel + "/") for rel in ws.links)


def reset(ws: Workspace) -> None:
    """Back to the base commit, in the task's worktree only."""
    _git(ws.path, "reset", "--hard", ws.base)
    _git(ws.path, "clean", "-fdq")
    link(ws)


def changed(ws: Workspace) -> tuple[str, ...]:
    """What the task changed against its base, from git: committed or not."""
    status = _git(ws.path, "status", "--porcelain", "-uall", "-z", strip=False)
    paths = {e[3:] for e in status.split("\0") if len(e) > 3}
    paths |= set(_git(ws.path, "diff", "--name-only", ws.base, "HEAD").splitlines())
    return tuple(sorted(p for p in paths if p and not _linked(ws, p)))


def has_changes(ws: Workspace) -> bool:
    return bool(changed(ws))


def finish(ws: Workspace, *, keep: bool, message: str) -> str | None:
    """Commit and keep the branch when `keep`, else drop both. Returns the branch kept."""
    kept_branch = None
    unlink(ws)
    if ws.links:
        # A worker that committed a link committed a pointer into this machine.
        _git(ws.path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *ws.links, check=False)
    if keep and has_changes(ws):
        if _git(ws.path, "status", "--porcelain"):
            _git(ws.path, "add", "-A")
            _commit(ws, message)
        elif UNVERIFIED in _git(ws.path, "log", "-1", "--format=%s") and UNVERIFIED not in message:
            # Work kept unverified has passed since, unchanged: the branch says so.
            _commit(ws, message, "--allow-empty")
        kept_branch = ws.branch
    _git(ws.repo_dir, "worktree", "remove", "--force", str(ws.path), check=False)
    if kept_branch is None:
        _git(ws.repo_dir, "branch", "-D", ws.branch, check=False)
    return kept_branch


def _commit(ws: Workspace, message: str, *extra: str) -> None:
    # cauce's own snapshot of a worker's work, for a person to review: a
    # pre-commit hook that needs approval or a missing install must not lose it.
    identity = [] if _git(ws.path, "config", "user.email", check=False) else [
        "-c", "user.name=cauce", "-c", "user.email=cauce@localhost"]
    _git(ws.path, *identity, "commit", "-q", "--no-verify", *extra, "-m", message)


def head(repo_dir: Path, ref: str) -> str:
    return _git(repo_dir, "rev-parse", ref)


def landed(repo_dir: Path, branch: str, paths: Sequence[str] = ()) -> bool:
    """Whether a kept branch needs no review any more: it is gone, it is in HEAD's
    history, or (a squash merge) HEAD holds what it changed, file for file."""
    if not _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        return True
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"],
                              cwd=str(repo_dir), capture_output=True, check=False)
    if ancestor.returncode == 0:
        return True
    if not paths:
        return False
    same = subprocess.run(["git", "diff", "--quiet", "HEAD", f"refs/heads/{branch}", "--", *paths],
                          cwd=str(repo_dir), capture_output=True, check=False)
    return same.returncode == 0



# --- one run at a time in a checkout ----------------------------------------
#
# A run without a worktree works in the person's checkout itself. Two of them at
# once share its files and its node_modules: one reinstalls while the other's
# check runs, and the check fails for a reason that is neither task's work.

#: How often a run that waits for the checkout looks again, and for a cancel.
WAIT_POLL_S = 2.0


def held_by(directory: Path, toplevel: Path | None) -> list[Path]:
    """What a run in `directory` holds: its checkout; or, in a folder that is no
    checkout, the folder and every checkout right inside it, since its work can
    reach them all. Sorted, so runs that hold several take them in one order and
    never wait on each other in a ring."""
    if toplevel is not None:
        return [toplevel.resolve()]
    inside = []
    with contextlib.suppress(OSError):
        inside = [child.resolve() for child in directory.iterdir() if (child / ".git").exists()]
    return sorted({directory.resolve(), *inside})


def checkout_lock(root: Path, checkout: Path) -> Path:
    digest = hashlib.sha256(str(checkout.resolve()).encode()).hexdigest()[:16]
    return root / "work" / f"checkout-{digest}.lock"


@contextlib.contextmanager
def in_place(root: Path, checkout: Path, task_id: int, *, waiting: Callable[[str], None],
             check: Callable[[], None]) -> Iterator[None]:
    """Holds `checkout` for task `task_id` while the block runs. Another run there
    holds it: `waiting` is told who, once, and the run waits, calling `check`
    (which raises to stop waiting, a cancel) between looks. Without `fcntl`, a
    no-op: the platform has no lock to take."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not on the platforms cauce runs on
        yield
        return
    path = checkout_lock(root, checkout)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        told = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not told:
                    fh.seek(0)
                    holder = fh.read().strip()
                    waiting(f"task #{holder}" if holder.isdigit() else "another run")
                    told = True
                check()
                time.sleep(WAIT_POLL_S)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(task_id))
            fh.flush()
            yield
        finally:
            fh.seek(0)
            fh.truncate()
            fcntl.flock(fh, fcntl.LOCK_UN)
