"""Merging the branches cauce kept into the person's checkout, on their word (`cauce merge`).

A task keeps its work on `cauce/task-N` and the person merges it. Several kept branches at
once are where the work meets: two that change the same file conflict when the second goes
in. `cauce merge 12 13 14` merges them into the branch checked out, one at a time, in an
order that puts the ones that touch no other first, and runs the check after each when one
is given. A conflict is aborted, never resolved by guess: the merge stops there, says the
files, and gives the task that would reconcile them. A check that fails after a merge stops
there too, with the merge in place and the line that undoes it.

Nothing here runs without the person's command; it never pushes, and it never rewrites a
commit that is already there.
"""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cauce import isolate

#: How long the check after one merge may run.
CHECK_TIMEOUT_S = 1800


class MergeError(ValueError):
    """What stops a merge before it starts: no checkout, local changes, a branch that is gone."""


@dataclass
class Merged:
    """What a merge of several kept branches came to, in order."""

    into: str
    merged: list[int] = field(default_factory=list)
    #: The task whose branch conflicted, and the files.
    conflict: tuple[int, list[str]] | None = None
    #: The task after whose merge the check failed, and the end of its output.
    failed: tuple[int, str] | None = None
    order: list[int] = field(default_factory=list)


def order(changed: Mapping[int, Sequence[str]]) -> list[int]:
    """The tasks in the order their branches go in: the ones whose files no other touches
    first, then by how few files they share, then by number. Pure."""
    def shared(task_id: int) -> int:
        mine = set(changed[task_id])
        return sum(len(mine & set(files)) for other, files in changed.items() if other != task_id)

    return sorted(changed, key=lambda t: (shared(t) > 0, shared(t), t))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def ready(top: Path, branches: Mapping[int, str]) -> str:
    """The branch checked out, once the checkout can take merges: tracked files unchanged,
    a branch (not a detached HEAD), and every branch still there. Raises `MergeError`."""
    into = isolate.current_branch(top)
    if not into:
        raise MergeError("the checkout is on no branch (a detached HEAD): check out the branch to merge into")
    dirty = _git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise MergeError("the checkout has uncommitted changes to tracked files: commit or stash them first")
    kept = isolate.kept_branches(top)
    gone = [f"#{t}" for t in branches if t not in kept]
    if gone:
        raise MergeError(f"no kept branch for {', '.join(gone)}: it was merged or pruned already")
    return into


def merge(top: Path, branches: Mapping[int, str], titles: Mapping[int, str], changed: Mapping[int, Sequence[str]],
          *, verify: str | None = None, say: Callable[[str], Any] = lambda line: None) -> Merged:
    """Merge each branch into the one checked out, in `order`, a merge commit each; the
    check after each when given. Stops at a conflict (aborted) or a failing check."""
    into = ready(top, branches)
    result = Merged(into=into, order=order({t: changed.get(t, ()) for t in branches}))
    for task_id in result.order:
        branch = branches[task_id]
        title = " ".join(str(titles.get(task_id) or "").split())[:72]
        done = _git(top, "merge", "--no-ff", "--no-edit", "-m", f"Merge {branch}: {title}".strip(), branch)
        if done.returncode != 0:
            files = [f for f in _git(top, "diff", "--name-only", "--diff-filter=U").stdout.split() if f]
            _git(top, "merge", "--abort")
            result.conflict = (task_id, files)
            return result
        result.merged.append(task_id)
        say(f"merged #{task_id} ({branch}) into {into}")
        if verify:
            try:
                ran = subprocess.run(verify, shell=True, cwd=str(top), capture_output=True, text=True,  # noqa: S602
                                     timeout=CHECK_TIMEOUT_S, check=False)
                code, out = ran.returncode, (ran.stdout + ran.stderr)
            except subprocess.TimeoutExpired:
                code, out = -1, f"the check ran past {CHECK_TIMEOUT_S}s"
            if code != 0:
                result.failed = (task_id, out.strip()[-1200:])
                return result
            say(f"  `{verify}` passes after it")
    return result


def resolve_task(into: str, branch: str, files: Sequence[str], verify: str | None) -> str:
    """The `cauce run` that reconciles a branch that conflicts with what was merged."""
    text = (f"Merge branch {branch} into {into} and resolve the conflicts in {', '.join(files[:8])}: keep what "
            f"each side meant to change; nothing else changes")
    return f"cauce run {shlex.quote(text)}" + (f" --verify {shlex.quote(verify)}" if verify else "")


def pr_body(task: Mapping[str, Any], final: Mapping[str, Any]) -> str:
    """A pull request's description from what cauce kept of a task: the worker's summary, the
    verdict (each criterion, the check cauce ran, the independent check) and the files."""
    from cauce.hooks import verdict_lines

    lines = [f"cauce task #{task['id']}: {' '.join(str(task['title'] or '').split())}", ""]
    if task.get("result"):
        lines += [str(task["result"]).strip(), ""]
    verdict = [line.strip() for line in verdict_lines(final.get("verdict"))
               if not line.strip().startswith("this verdict is cauce's own record")]
    if verdict:
        lines += ["## Verdict", *(f"- {line}" for line in verdict), ""]
    changed = list(final.get("changed") or [])
    if changed:
        lines += ["## Changed (from git)", *(f"- `{p}`" for p in changed[:40])]
        if len(changed) > 40:
            lines.append(f"- and {len(changed) - 40} more")
    return "\n".join(lines).strip() + "\n"


def open_pr(top: Path, branch: str, title: str, body: str, *, base: str | None = None,
            run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> tuple[bool, str]:
    """Push `branch` and open a draft pull request for it with `gh`: (opened, what it said)."""
    pushed = run(["git", "push", "-u", "origin", branch], cwd=str(top), capture_output=True, text=True, check=False)
    if pushed.returncode != 0:
        return False, f"git push failed: {(pushed.stderr or pushed.stdout).strip()[-400:]}"
    argv = ["gh", "pr", "create", "--draft", "--head", branch, "--title", title, "--body", body]
    if base:
        argv += ["--base", base]
    try:
        made = run(argv, cwd=str(top), capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"gh did not run: {exc}"
    said = (made.stdout or made.stderr).strip()
    return made.returncode == 0, said[-400:]
