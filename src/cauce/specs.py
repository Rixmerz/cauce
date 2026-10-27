"""Whether a repository's specs keep up with its code: read from git, never a model.

A repository that keeps its behaviour in OpenSpec (`openspec/`) and lets its code move on without
it ends up with specs that describe a product it no longer is. cauce reads those specs to brief
and assess, so it is the one place that sees every change land; this module says how far the
specs are behind, tells a writing worker the repository keeps them, and gives the ending the
line that would bring them up to date. It starts nothing and writes nothing.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

#: The folder OpenSpec keeps a repository's specs and proposed changes in.
OPENSPEC = "openspec"
#: Code commits since the specs last changed from which they are said to be behind.
BEHIND_COMMITS = 5
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


#: What every line `link_lines` writes begins with: the independent check's brief and the ending tell them apart by it.
LINK_PREFIX = "spec link: "
#: A requirement of an OpenSpec spec file, and the section of a change's delta it sits under.
_REQUIREMENT = re.compile(r"^###\s+Requirement:\s*(?P<name>.+?)\s*$")
_DELTA = re.compile(r"^##\s+(?P<verb>ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,3}\s")
#: The id `export_openspec` writes under a requirement's heading; livespec prefers it to the one the heading derives.
_ID_MARKER = re.compile(r"^<!--\s*livespec:id=(?P<id>[^\s>]+)\s*-->$", re.IGNORECASE)
#: Letters NFKD leaves whole, which livespec folds to ASCII by hand.
_FOLDED = str.maketrans({"ß": "ss", "æ": "ae", "ø": "o", "œ": "oe", "đ": "d", "ð": "d", "ł": "l", "þ": "th",
                         "\u0131": "i"})
#: An `@spec:<id>` (or `@spec=<id>`) comment, as livespec reads it; several ids may follow one.
_ANNOTATION = re.compile(r"@spec\s*[:=]\s*(?P<rest>[^\n\r@]*)")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


@dataclass(frozen=True)
class Requirement:
    """A requirement a change wrote or changed in a spec file, under the id livespec gives it."""

    spec_id: str
    path: str
    #: Written by the change (absent from the spec file before it), else already there.
    new: bool
    #: The id livespec derived before it folded accents, when that is another: it still answers to it in `@spec:`.
    legacy: str = ""


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


def coded(changed: Sequence[str]) -> list[str]:
    """The files of a change that may hold behaviour: not a spec, not a document."""
    return [p for p in changed if not (p == OPENSPEC or p.startswith(OPENSPEC + "/") or p.startswith("docs/")
                                       or p.lower().endswith((".md", ".mdx", ".rst", ".txt")))]


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


def slug(text: str, *, folded: bool = True) -> str:
    """A name as livespec reads it into a spec id: accents folded to ASCII, lower case, runs of anything
    but letters and digits as one hyphen, no hyphen at the edges. `folded=False` is the slug as it was before
    livespec folded accents (`validación` -> `validaci-n`): the ids old `@spec:` comments still carry."""
    text = text.strip().lower()
    if folded:
        text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
        text = text.translate(_FOLDED)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "requirement"


def _spec_file(path: str) -> bool:
    parts = path.split("/")
    return parts[0] == OPENSPEC and parts[-1] == "spec.md" and len(parts) >= 3


class _Found(NamedTuple):
    spec_id: str
    first: int
    last: int
    #: In a delta's ADDED section, or in a canonical spec.
    added: bool
    legacy: str


def _requirements(path: str, text: str) -> list[_Found]:
    """The requirements a spec file holds, those a change removes or renames left out. The id is the one
    livespec gives: the capability's folder and the requirement's name, or the `livespec:id` marker under
    the heading when there is one."""
    capability = path.split("/")[-2]
    found: list[_Found] = []
    delta: str | None = None
    open_at: _Found | None = None
    leading = False
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        heading = _REQUIREMENT.match(line)
        if heading or _HEADING.match(line):
            if open_at is not None:
                found.append(open_at._replace(last=number - 1))
                open_at = None
            if (section := _DELTA.match(line)) is not None:
                delta = section["verb"].upper()
            elif line.startswith("## "):
                delta = None
            if heading and delta not in ("REMOVED", "RENAMED"):
                spec_id = f"{slug(capability)}-{slug(heading['name'])}"
                old = f"{slug(capability, folded=False)}-{slug(heading['name'], folded=False)}"
                open_at = _Found(spec_id, number, 0, delta in (None, "ADDED"), old if old != spec_id else "")
                leading = True
        elif open_at is not None and leading and line.strip():
            # The marker counts only before the requirement's prose, as livespec reads it.
            leading = False
            if (marker := _ID_MARKER.match(line.strip())) is not None:
                open_at = open_at._replace(spec_id=marker["id"], legacy="")
    if open_at is not None:
        found.append(open_at._replace(last=len(lines)))
    return found


def _git_text(workdir: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _changed_lines(workdir: Path, ref: str, path: str, now: str) -> set[int]:
    """The lines of `path` a change added or rewrote, as they are now. A blank line is no change to
    a requirement: one added after the last of them belongs to what is written next."""
    kept = now.splitlines()
    lines: set[int] = set()
    for row in (_git_text(workdir, "diff", "-U0", ref, "--", path) or "").splitlines():
        if (hunk := _HUNK.match(row)) is not None:
            start, count = int(hunk["start"]), int(hunk["count"] or 1)
            lines.update(n for n in range(start, start + count) if n <= len(kept) and kept[n - 1].strip())
    return lines


def requirements_touched(workdir: Path, base: str | None, changed: Sequence[str]) -> list[Requirement]:
    """The requirements of the spec files a change wrote or changed, against `base` (HEAD when the
    change is uncommitted): the ones whose lines it touched. `new` is a requirement the change wrote;
    one a delta MODIFIES, or a canonical spec already had, is not."""
    ref = base or "HEAD"
    out: list[Requirement] = []
    for path in changed:
        if not _spec_file(path) or not (workdir / path).is_file():
            continue
        now = (workdir / path).read_text(encoding="utf-8", errors="replace")
        before = _git_text(workdir, "show", f"{ref}:{path}")
        old_ids = {r.spec_id for r in _requirements(path, before)} if before is not None else set()
        touched_lines = _changed_lines(workdir, ref, path, now) if before is not None else None
        for r in _requirements(path, now):
            if touched_lines is not None and touched_lines.isdisjoint(range(r.first, r.last + 1)):
                continue
            out.append(Requirement(r.spec_id, path, r.added and r.spec_id not in old_ids, r.legacy))
    return out


def annotated(workdir: Path) -> set[str]:
    """The spec ids any code of the work directory carries in an `@spec:` comment: tracked and new
    files alike, never the specs themselves nor a document."""
    shown = _git_text(workdir, "grep", "--untracked", "-I", "-h", "-e", "@spec", "--", ".",
                      f":(exclude){OPENSPEC}", ":(exclude)*.md") or ""
    return {token.lower() for match in _ANNOTATION.finditer(shown)
            for token in re.findall(r"[A-Za-z0-9][\w-]*", match["rest"])}


def unlinked(workdir: Path, base: str | None, changed: Sequence[str]) -> list[Requirement]:
    """The requirements a change wrote or changed that no code names in an `@spec:` comment."""
    if not any(_spec_file(p) for p in changed):
        return []
    found = requirements_touched(workdir, base, changed)
    if not found:
        return []
    carried = annotated(workdir)
    return [r for r in found if not carried & {r.spec_id.lower(), r.legacy}]


#: What a check of a change that writes or changes specs is told, so it verifies what the worker was asked to
#: do (`Livespec.spec_links_brief`): the code that implements a spec names it, and the code index reads that.
LINK_CHECK = ("The change writes or changes OpenSpec specs, and the code that implements a spec carries an "
              "`@spec:<id>` comment (the id is `<capability>-<requirement-name>` with accents folded, or the id of a "
              "`<!-- livespec:id=… -->` marker under the heading; the project's code index links spec and code by "
              "it). Check that each spec the change touched has one on the functions that "
              "implement it: a spec the change wrote with none is a finding that blocks, with `git grep -n "
              "'@spec:<id>'` showing none as its proof; a spec that already existed with none is a finding whose "
              "cause is `pre-existing`: a warning, never a blocker.")


def links_expected(repo_dir: Path | str | None, env: Mapping[str, str]) -> bool:
    """Whether workers here were told to link the specs they write to their code: a repository that keeps
    OpenSpec specs and has a livespec index, with the `specs` and `livespec` settings on. Read from the
    files and the settings, never a call to livespec."""
    from cauce import config, repo
    from cauce.adapters.livespec import Livespec

    top = repo.toplevel(Path(repo_dir)) if repo_dir else None
    return (top is not None and (top / OPENSPEC).is_dir() and Livespec.db_path(top).is_file()
            and config.enabled("specs", env) and config.enabled("livespec", env))


def link_lines(found: Sequence[Requirement]) -> list[str]:
    """What a change owes the code index, as lines for the ending and the independent check: a spec it
    wrote with no `@spec:` comment on any code blocks, and one that was already there is a warning."""
    lines = []
    for r in found:
        did, outcome = (("wrote", "blocks, the functions that implement it need the comment") if r.new
                        else ("changed", "a warning, not a blocker: the spec was already there"))
        lines.append(f"{LINK_PREFIX}the change {did} `{r.spec_id}` ({r.path}) and no code carries "
                     f"`@spec:{r.spec_id}`: {outcome}")
    return lines
