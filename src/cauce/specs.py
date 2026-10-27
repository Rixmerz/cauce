"""Whether a repository's specs keep up with its code: read from git, never a model.

A repository that keeps its behaviour in OpenSpec (`openspec/`) and lets its code move on without
it ends up with specs that describe a product it no longer is. cauce reads those specs to brief
and assess, so it is the one place that sees every change land; this module says how far the
specs are behind, tells a writing worker the repository keeps them, and gives the ending the
line that would bring them up to date. It starts nothing and writes nothing.
"""
from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The folder OpenSpec keeps a repository's specs and proposed changes in.
OPENSPEC = "openspec"
#: Code commits since the specs last changed from which they are said to be behind.
BEHIND_COMMITS = 5


@dataclass(frozen=True)
class Drift:
    """How far a repository's specs are behind its code at a commit."""

    folder: str
    #: The last commit that touched the specs, and how many days before `ref` it was.
    last: str | None
    days: int | None
    #: Commits since then that changed anything outside the specs.
    code_commits: int

    @property
    def behind(self) -> bool:
        return self.last is None or self.code_commits >= BEHIND_COMMITS

    def text(self) -> str:
        if self.last is None:
            return f"`{self.folder}/` was never committed"
        return (f"`{self.folder}/` last changed {self.days} day(s) and {self.code_commits} code commit(s) ago "
                f"(at {self.last[:7]})")

    def data(self) -> dict:
        return {"folder": self.folder, "last": self.last, "days": self.days, "code_commits": self.code_commits}


def _git(repo_dir: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def drift(repo_dir: Path, ref: str = "HEAD") -> Drift | None:
    """How far `openspec/` is behind the code at `ref`; None where the repository keeps no
    OpenSpec folder or git cannot say."""
    if not (repo_dir / OPENSPEC).is_dir():
        return None
    head = _git(repo_dir, "log", "-1", "--format=%ct", ref)
    if not head:
        return None
    last = _git(repo_dir, "log", "-1", "--format=%H %ct", ref, "--", OPENSPEC)
    if not last:
        count = _git(repo_dir, "rev-list", "--count", ref)
        return Drift(OPENSPEC, None, None, int(count) if count and count.isdigit() else 0)
    sha, stamp = last.split()
    count = _git(repo_dir, "rev-list", "--count", f"{sha}..{ref}", "--", ".", f":(exclude){OPENSPEC}")
    days = max(0, (int(head) - int(stamp)) // 86400)
    return Drift(OPENSPEC, sha, days, int(count) if count and count.isdigit() else 0)


def touched(changed: Sequence[str]) -> bool:
    """Whether a change wrote any spec."""
    return any(p == OPENSPEC or p.startswith(OPENSPEC + "/") for p in changed)


def brief_lines(found: Drift) -> list[str]:
    """What a writing worker is told: the repository keeps its behaviour in specs, and a change of
    behaviour updates them in the same work. Data, as every brief line about the repository."""
    lines = [f"This repository keeps its behaviour in OpenSpec specs (`{found.folder}/`). A change in "
             f"behaviour updates the spec that describes it in the same work, or proposes one under "
             f"`{found.folder}/changes/`; a behaviour with no spec gets one."]
    if found.behind:
        lines.append(f"Its specs are behind the code: {found.text()}. Do not rewrite the ones your change "
                     "does not reach; say in your result which behaviour you saw without a spec.")
    return lines


def ask(found: Drift) -> str:
    """The request that would bring the specs up to date, for an ending to offer."""
    since = f" since {found.last[:7]}" if found.last else ""
    return (f"Bring {found.folder}/ up to date with the code: write OpenSpec specs for the behaviour built"
            f"{since} that no spec describes (livespec's propose_specs_from_codebase drafts them from the "
            f"code), mark the ones that no longer hold, and validate with `openspec validate --all --strict`. "
            f"Change no code.")


def ending_lines(task: Mapping, changed: Sequence[str], found: Mapping | None, *, after: str) -> list[str]:
    """What an ending says about specs: a writing pass that changed code and no spec, in a
    repository whose specs are behind, with the task that would catch them up; nothing started."""
    if not found or not changed or touched(changed):
        return []
    behind = Drift(str(found.get("folder") or OPENSPEC), found.get("last"), found.get("days"),
                   int(found.get("code_commits") or 0))
    if not behind.behind:
        return []
    return [f"  its change touches no spec, and {behind.text()}: the specs no longer describe the code. "
            f"To catch them up (yours to decide, nothing was started): "
            f"`cauce queue add --kind docs --after {after} {shlex.quote(ask(behind))}`"]
