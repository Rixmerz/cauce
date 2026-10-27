"""Whether a repository's specs keep up with its code: read from git, never a model.

A repository that keeps its behaviour in OpenSpec (`openspec/`) and lets its code move on without
it ends up with specs that describe a product it no longer is. cauce reads those specs to brief
and assess, so it is the one place that sees every change land; this module says how far the
specs are behind, tells a writing worker the repository keeps them, and gives the ending the
line that would bring them up to date. It starts nothing and writes nothing.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cauce import config
from cauce.adapters.livespec import Livespec, LivespecError, is_test_path

#: The folder OpenSpec keeps a repository's specs and proposed changes in.
OPENSPEC = "openspec"
#: Code commits since the specs last changed from which they are said to be behind.
BEHIND_COMMITS = 5
#: Kinds of structural finding `structure_lines` names; the rest are counted.
FINDINGS_SHOWN = 8
#: The repositories a person made spec-first (`cauce specs first on`), kept per repository key in
#: the store's meta: the person's cauce home, never a repository, which cannot make its own workers
#: answer to a rule by committing a file.
FIRST_META = "specs_first:"
#: The kinds that build behaviour, and so owe its spec where a repository is spec-first. A fix
#: restores what a spec already says; a refactor, a test or a document changes no behaviour.
FIRST_KINDS = frozenset({"feature", "implement"})
#: What a pass that wrote no spec is held back by, the first time (`WorkerResult.shape`).
FIRST_SHAPE = "spec-first"
#: What the same worker is told when it goes on in its own session to write the spec.
FIRST_PROMPT = ("This repository is spec-first, and your work changed its code but wrote no spec; why is below. Your "
                "work so far is in this directory and stands: do not redo it. Write or update the OpenSpec spec of "
                "the behaviour you built (a change under `openspec/changes/<name>/`, or the spec under "
                "`openspec/specs/` it reaches), check it, and answer again in the result contract you were given.")


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


#: Files that hold no behaviour a spec describes, though they are not documents. A lockfile pins what
#: a manifest already says; a config file sets a tool or a deploy up; a generated file is built from
#: another; a test checks behaviour a spec states. A build that changes only these owes no spec.
_LOCKFILE = re.compile(r"\.lockb?$|(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|go\.sum)$")
_CONFIG_FILE = re.compile(
    r"\.(jsonc?|ya?ml|toml|ini|cfg|conf|properties|env)$|(^|/)\.[^/]+$|(^|/)(Dockerfile|Makefile|Procfile)$|"
    r"\.config\.[cm]?[jt]s$|(^|/)(\.github|\.vscode|\.idea)/")
#: `build`, `out` and `target` are output only at the root, where the build tools put it: below it they
#: are ordinary names (`src/build/compiler.py`, `lib/out/render.ts`, `internal/target/plan.go`).
_GENERATED = re.compile(
    r"(^|/)(dist|vendor|node_modules|coverage|__pycache__|__generated__|generated|__snapshots__)/|"
    r"^(build|out|target)/|"
    r"\.(min\.(js|css)|map|snap|pb\.go)$|\.generated\.|_pb2(_grpc)?\.pyi?$")


def holds_behaviour(path: str) -> bool:
    """Whether a changed file may hold behaviour a spec would describe: not a spec, a document, a
    test, a lockfile, a config file or a generated file."""
    return not (path == OPENSPEC or path.startswith(OPENSPEC + "/") or path.startswith("docs/")
                or path.lower().endswith((".md", ".mdx", ".rst", ".txt")) or is_test_path(path)
                or path.rsplit("/", 1)[-1] == "conftest.py" or _LOCKFILE.search(path)
                or _CONFIG_FILE.search(path) or _GENERATED.search(path))


def coded(changed: Sequence[str]) -> list[str]:
    """The files of a change that may hold behaviour (`holds_behaviour`)."""
    return [p for p in changed if holds_behaviour(p)]


def first(store: Any, key: str | None) -> bool:
    """Whether the person made this repository spec-first."""
    return bool(key) and store.meta(FIRST_META + str(key)) == "on"


def set_first(store: Any, key: str | None, on: bool) -> None:
    store.set_meta(FIRST_META + str(key), "on" if on else "off")


def owed(kind: str, changed: Sequence[str]) -> list[str]:
    """In a spec-first repository, the files of a build's change that owe a spec it did not write:
    empty when the change wrote one, or holds no code, or is no build."""
    if kind not in FIRST_KINDS or touched(changed):
        return []
    return coded(changed)


def first_shape(paths: Sequence[str]) -> str:
    """Why a pass that owes a spec is held back, in the words its worker is given."""
    shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return (f"{FIRST_SHAPE}: the change touches code ({shown}) and no file under `{OPENSPEC}/`; this repository "
            "accepts no change of behaviour without its spec")


def structure_lines(result: Mapping[str, Any]) -> list[str]:
    """What livespec's `validate_openspec` says of the specs' structure (every requirement a
    scenario, a title, a body, normative words), one line a finding; the problems only."""
    errors, warnings = int(result.get("error_count") or 0), int(result.get("warning_count") or 0)
    if not errors and not warnings:
        return ["structure: livespec's validate_openspec found no structural problem"]
    found = [f for f in result.get("findings") or [] if isinstance(f, Mapping)]
    lines = [f"structure: livespec's validate_openspec found {errors} error(s) and {warnings} warning(s)"]
    for f in found[:FINDINGS_SHOWN]:
        ids = ", ".join(str(x.get("spec_id")) for x in (f.get("sample") or [])[:3] if isinstance(x, Mapping))
        lines.append(f"  - {f.get('severity', 'warning')}: {f.get('issue', '?')} ({f.get('count', '?')})"
                     + (f" e.g. {ids}" if ids else ""))
    if len(found) > FINDINGS_SHOWN:
        lines.append(f"  - and {len(found) - FINDINGS_SHOWN} more kind(s) of finding")
    return lines


def structure(repo_dir: Path, adapter: Livespec | None = None) -> list[str]:
    """The structure of the specs as livespec sees it, where livespec runs here: nothing when it
    does not (off, or no way to run it), and one line saying so when it could not be asked. The tool
    reads livespec's index, so a repository with none is indexed first, within the seconds a first
    task waits for one (`index_wait`)."""
    if not config.enabled("livespec", os.environ):
        return []
    adapter = adapter or Livespec()
    if adapter.server() is None:
        return []
    if not adapter.inspect(repo_dir).present:
        wait_s = config.index_wait(os.environ)
        outcome, said = adapter.refresh_within(repo_dir, wait_s) if wait_s else ("off", "index_wait is 0")
        if outcome != "done":
            return [f"structure: not validated, livespec has no index here yet ({said}); "
                    "the next task in this repository indexes it"]
    try:
        return structure_lines(adapter.validate_openspec(repo_dir))
    except LivespecError as exc:
        return [f"structure: livespec could not validate the specs ({exc})"]


def brief_lines(found: Drift, *, first: bool = False) -> list[str]:
    """What a writing worker is told: the repository keeps its behaviour in specs, and a change of
    behaviour updates them in the same work. Data, as every brief line about the repository."""
    lines = [f"This repository keeps its behaviour in OpenSpec specs (`{found.folder}/`). A change in "
             f"behaviour updates the spec that describes it in the same work, or proposes one under "
             f"`{found.folder}/changes/`; a behaviour with no spec gets one."]
    if first:
        lines.append(f"This repository is spec-first: write the spec of the behaviour before its code, and keep "
                     f"them together. Work that builds behaviour and writes nothing under `{found.folder}/` is "
                     "not accepted, whatever its check says.")
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
