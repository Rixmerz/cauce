"""One git worktree per task, outside the repository.

A task that writes code runs in its own worktree on its own branch. The user's
checkout is never touched while the attempts run, and a task that passed leaves
exactly one branch to merge.

A task that stopped for a person — blocked on a refused command,
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
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Folders a build needs and git does not carry. Linked, not copied: shared with
#: the checkout, which is why the brief tells the worker not to reinstall them.
DEPENDENCY_DIRS = frozenset({"node_modules", ".venv", "venv"})
#: How the commit of work kept without a pass is marked: a trailer, so the
#: subject stays the change's. `UNVERIFIED_BEFORE` is how a subject said it once.
UNVERIFIED = "Cauce-Verified: no"
UNVERIFIED_BEFORE = "(unverified"
#: The trailer that ties a commit to its task.
TASK_TRAILER = "Cauce-Task"
#: How long a commit's subject and the summary under it may be.
SUBJECT_CHARS, BODY_CHARS = 72, 700
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


def last_commit(repo_dir: Path, branch: str) -> str | None:
    """`<sha> <subject>` of a branch's newest commit, or None when it is gone."""
    return _git(repo_dir, "log", "-1", "--format=%h %s", f"refs/heads/{branch}", "--", check=False) or None


def kept(repo_dir: Path, task_id: int) -> bool:
    """Whether the task's branch exists: work it kept, verified or not."""
    return bool(_git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_for(task_id)}", check=False))


def _attached(top: Path, path: Path, branch: str) -> bool:
    """Whether `path` is already a worktree of `top` on `branch`."""
    listing = _git(top, "worktree", "list", "--porcelain", check=False) or ""
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        here = f"worktree {path}" in lines or f"worktree {path.resolve()}" in lines
        if here and f"branch refs/heads/{branch}" in lines:
            return True
    return False


def prepare(repo_dir: Path, task_id: int, root: Path, *, base: str | None = None) -> Workspace:
    """The task's worktree, on its own branch: from HEAD, or from `base` (a
    commit a workflow step builds on) when it names one that exists."""
    top = Path(_git(repo_dir, "rev-parse", "--show-toplevel"))
    head = _git(top, "rev-parse", "HEAD")
    if base:
        head = _git(top, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False) or head
    branch = branch_for(task_id)
    path = root / "worktrees" / f"task-{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(top, "worktree", "prune", check=False)  # a run that died left its entry behind
    if _attached(top, path, branch):
        # A run whose process died left its worktree, uncommitted work and all:
        # the resume continues in it instead of failing to add it again.
        base = _git(top, "merge-base", head, branch)
    elif kept(top, task_id):
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


#: Where the work an attempt left is set aside before a reset: a ref per attempt, so
#: the next model starts clean and can still read what the last one did.
ASIDE = "refs/cauce/aside"


def set_aside(ws: Workspace, attempt: int) -> str | None:
    """Keep the worktree's work as it is on a ref of its own, before a reset throws
    it away. None when there is nothing to keep."""
    if not has_changes(ws):
        return None
    _git(ws.path, "add", "-A")
    tree = _git(ws.path, "write-tree")
    commit = _git(ws.path, *_identity(ws.path), "commit-tree", tree, "-p", _git(ws.path, "rev-parse", "HEAD"),
                  "-m", f"cauce: what attempt {attempt} left, set aside before a reset")
    ref = f"{ASIDE}/{ws.branch.rsplit('/', 1)[-1]}-{attempt}"
    _git(ws.path, "update-ref", ref, commit)
    return ref


def drop_aside(ws: Workspace) -> None:
    """Forget the work set aside for this task: nothing will resume from it."""
    prefix = f"{ASIDE}/{ws.branch.rsplit('/', 1)[-1]}-"
    for ref in _git(ws.repo_dir, "for-each-ref", "--format=%(refname)", ASIDE, check=False).split():
        if ref.startswith(prefix):
            _git(ws.repo_dir, "update-ref", "-d", ref, check=False)


def dirty(top: Path) -> list[str]:
    """What a checkout holds that is not committed: changed, added, deleted or untracked
    (ignored files aside)."""
    status = _git(top, "status", "--porcelain", "-uall", "-z", check=False, strip=False)
    return sorted({e[3:] for e in status.split("\0") if len(e) > 3 and e[2] == " " and not is_artifact(e[3:])})


def named_in(text: str, paths: Sequence[str]) -> list[str]:
    """The paths a task's text names: by path, or by a file name of four characters or more."""
    low = text.lower()
    out = []
    for p in paths:
        name = Path(p).name.lower()
        if p.lower() in low or (len(name) >= 4 and re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", low)):
            out.append(p)
    return out


def carry(ws: Workspace, paths: Sequence[str]) -> list[str]:
    """Bring the person's uncommitted state of `paths` into the worktree, a deletion as
    a deletion: work the task builds on that no commit holds yet."""
    carried = []
    for rel in paths:
        src, dst = ws.repo_dir / rel, ws.path / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif not src.exists() and dst.is_file():
            dst.unlink()
        else:
            continue
        carried.append(rel)
    return carried


#: A path-looking word of a task's text: `scripts/slides/`, `config.local.json`.
PATHLIKE = re.compile(r"(?<![\w/.-])((?:\.?[\w@-]+/)+[\w.@-]*|[\w-]+\.[A-Za-z0-9]{1,8})(?![\w/-])")
#: The most a copied ignored path may weigh: past it, the task says so instead.
BRING_BYTES = 50 * 1024 * 1024


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / f).stat().st_size
            if total > BRING_BYTES:
                return total
    return total


def ignored_named(ws: Workspace, text: str) -> list[str]:
    """Paths the task's text names that the person's checkout has, git ignores, and the
    worktree therefore lacks."""
    out = []
    for word in sorted(set(PATHLIKE.findall(text))):
        rel = word.strip("/").removeprefix("./")
        if not rel or ".." in rel.split("/") or not (ws.repo_dir / rel).exists() or (ws.path / rel).exists():
            continue
        if subprocess.run(["git", "check-ignore", "-q", rel], cwd=str(ws.repo_dir), check=False).returncode == 0:
            out.append(rel)
    return out


def bring(ws: Workspace, rels: Sequence[str]) -> tuple[list[str], list[str]]:
    """Copy ignored paths from the person's checkout into the worktree: (copied, too big).
    Copies, never links: what the worker does to them stays out of the person's files,
    and being ignored they are never committed."""
    copied, big = [], []
    for rel in rels:
        src, dst = ws.repo_dir / rel, ws.path / rel
        if _size(src) > BRING_BYTES:
            big.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied, big


def reset(ws: Workspace) -> None:
    """Back to the base commit, in the task's worktree only."""
    _git(ws.path, "reset", "--hard", ws.base)
    _git(ws.path, "clean", "-fdq")
    link(ws)


#: Folders a worker's tools write for their own use (a browser server's page
#: snapshots and console logs, an interpreter's bytecode). They are no work: never a changed file, never
#: committed to a kept branch.
ARTIFACT_DIRS = frozenset({".playwright-mcp", "__pycache__"})


def is_artifact(path: str) -> bool:
    return any(part in ARTIFACT_DIRS for part in path.split("/")[:-1])


def changed(ws: Workspace) -> tuple[str, ...]:
    """What the task changed against its base, from git: committed or not."""
    # A status that never takes the index lock: a watcher's read must not fail the
    # worker's own `git add`, nor cauce's commit at the end of the run.
    status = _git(ws.path, "--no-optional-locks", "status", "--porcelain", "-uall", "-z", strip=False)
    paths = {e[3:] for e in status.split("\0") if len(e) > 3}
    paths |= set(_git(ws.path, "diff", "--name-only", ws.base, "HEAD").splitlines())
    return tuple(sorted(p for p in paths if p and not _linked(ws, p) and not is_artifact(p)))


def changed_so_far(root: Path, repo_dir: Path, task_id: int, started: Mapping[str, Any]) -> tuple[str, ...] | None:
    """What a running task changed so far in its worktree, against the base its
    attempt started from (`started` is its `attempt_started` event's data, which
    names the base and the linked dependencies). None when git cannot say: no
    worktree, no base, a git that failed."""
    path = root / "worktrees" / f"task-{task_id}"
    if not path.is_dir() or not started.get("base"):
        return None
    ws = Workspace(repo_dir, path, branch_for(task_id), str(started["base"]), links=tuple(started.get("links") or ()))
    try:
        return changed(ws)
    except (IsolationError, OSError):
        return None


def undo(ws: Workspace, paths: Sequence[str]) -> None:
    """Put these paths back as they were at the base, in the task's worktree only:
    a file the base had is restored, one it did not have is removed."""
    for path in paths:
        known = subprocess.run(["git", "cat-file", "-e", f"{ws.base}:{path}"], cwd=str(ws.path),
                               capture_output=True, check=False).returncode == 0
        if known:
            _git(ws.path, "checkout", ws.base, "--", path)
        else:
            target = ws.path / path
            if target.is_file() or target.is_symlink():
                target.unlink()


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
            _git(ws.path, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                 *(f":(glob)**/{d}/**" for d in sorted(ARTIFACT_DIRS)), check=False)
            _commit(ws, message)
        elif _unverified(_git(ws.path, "log", "-1", "--format=%B")) and not _unverified(message):
            # Work kept unverified has passed since, unchanged: the branch says so.
            _commit(ws, message, "--allow-empty")
        kept_branch = ws.branch
    _git(ws.repo_dir, "worktree", "remove", "--force", str(ws.path), check=False)
    if kept_branch is None:
        _git(ws.repo_dir, "branch", "-D", ws.branch, check=False)
        drop_aside(ws)
    return kept_branch


def _unverified(message: str) -> bool:
    return UNVERIFIED in message or UNVERIFIED_BEFORE in message.split("\n", 1)[0]


#: Words a cut subject must not end on: the thought they open is the part that was cut.
_DANGLING = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "its", "his", "her", "their", "our", "your", "my",
    "each", "every", "all", "any", "no", "not", "and", "or", "but", "so", "if", "when", "while", "because",
    "which", "where", "than", "then", "as", "at", "by", "for", "from", "in", "into", "of", "on", "to", "with",
    "via", "per", "after", "before", "over", "under", "about", "between", "through", "during", "until", "upon",
    "is", "are", "was", "were", "be", "been", "will", "would", "should", "can", "could"})


def _subject_of(text: str) -> str:
    """A commit subject from one line: itself when it fits, else cut at the last
    whole word within `SUBJECT_CHARS`, before a word that opens a clause the cut
    left out and before a parenthesis it left open. Never an ellipsis: git
    history is read as sentences."""
    if len(text) <= SUBJECT_CHARS:
        return text
    cut = text[:SUBJECT_CHARS]
    if text[SUBJECT_CHARS] != " ":
        cut = cut.rpartition(" ")[0] or cut  # no space to cut at: the limit is the cut
    words = cut.split()
    while True:
        while words and words[-1].lower().strip(",;:") in _DANGLING:
            words.pop()
        kept = " ".join(words)
        if kept.count("(") <= kept.count(")"):
            break
        words = kept.rpartition("(")[0].split()
    # Nothing but dangling words or an open clause: the whole-word cut is the subject.
    return kept.rstrip(" ,;:-–—(") or cut


def message(task_id: int, *, title: str, subject: str = "", summary: str = "", status: str = "done") -> str:
    """The commit message of a task's work, as a project's history reads it: the
    change in one line (the worker's, else the task's title), what it does, and
    cauce's trailers: the task, and whether it was verified."""
    import textwrap

    from cauce.notes import _SECRET

    whole = _SECRET.sub("[secret]", " ".join((subject or "").split()) or " ".join((title or "").split()))
    whole = whole or f"Work of cauce task #{task_id}"
    line = _subject_of(whole)
    parts = [line]
    # A subject that had to be cut is given whole first in the body: the history keeps what was asked.
    if line != whole:
        parts.append(textwrap.fill(whole, 72))
    body = _SECRET.sub("[secret]", " ".join((summary or "").split()))
    if body:
        body = body if len(body) <= BODY_CHARS else body[:BODY_CHARS - 1].rsplit(" ", 1)[0] + "…"
        parts.append(textwrap.fill(body, 72))
    trailers = [f"{TASK_TRAILER}: #{task_id}"]
    if status != "done":
        trailers.append(f"{UNVERIFIED} ({status})")
    parts.append("\n".join(trailers))
    return "\n\n".join(parts)


def _identity(path: Path) -> list[str]:
    return [] if _git(path, "config", "user.email", check=False) else [
        "-c", "user.name=cauce", "-c", "user.email=cauce@localhost"]


def _commit(ws: Workspace, message: str, *extra: str) -> None:
    # cauce's own snapshot of a worker's work, for a person to review: a
    # pre-commit hook that needs approval or a missing install must not lose it.
    _git(ws.path, *_identity(ws.path), "commit", "-q", "--no-verify", *extra, "-m", message)


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



def kept_branches(repo_dir: Path) -> dict[int, str]:
    """Every `cauce/task-<id>` branch in a checkout, by task id, with its tip."""
    out = _git(repo_dir, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/cauce/", check=False)
    found = {}
    for line in out.splitlines():
        name, _, tip = line.partition(" ")
        number = name.removeprefix("cauce/task-")
        if number.isdigit() and tip:
            found[int(number)] = tip
    return found


def tree_of(repo_dir: Path, ref: str) -> str:
    """The tree a commit holds: two branches with one tree hold the same work."""
    return _git(repo_dir, "rev-parse", f"{ref}^{{tree}}", check=False)


def prunable(repo_dir: Path, branch: str) -> str | None:
    """Why a kept branch can be deleted with nothing lost, or None. Its commits are
    on another branch (a merge); or HEAD holds every file it changed, as it
    changed them (a squash or a cherry-pick); or all it changed is tool artifacts.
    A branch checked out anywhere, or only on a remote's `cauce/` copy, stays."""
    tip = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if not tip:
        return None
    if f"branch refs/heads/{branch}\n" in _git(repo_dir, "worktree", "list", "--porcelain", strip=False, check=False):
        return None
    holders = _git(repo_dir, "for-each-ref", "--contains", tip, "--format=%(refname)", "refs/heads", "refs/remotes",
                   check=False).splitlines()
    if any("/cauce/task-" not in ref and not ref.endswith("/HEAD") for ref in holders):
        return "merged"
    base = _git(repo_dir, "merge-base", "HEAD", tip, check=False)
    if not base:
        return None
    paths = _git(repo_dir, "diff", "--name-only", base, tip, check=False).splitlines()
    if paths and all(is_artifact(path) for path in paths):
        return "artifacts only"
    same = subprocess.run(["git", "diff", "--quiet", "HEAD", tip, "--", *paths], cwd=str(repo_dir),
                          capture_output=True, check=False)
    return "squash-merged" if paths and same.returncode == 0 else None


def delete_branch(repo_dir: Path, branch: str) -> bool:
    return subprocess.run(["git", "branch", "-D", branch], cwd=str(repo_dir), capture_output=True,
                          check=False).returncode == 0


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
