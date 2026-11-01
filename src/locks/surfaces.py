"""Where a task may write: its edit surfaces, paths and globs a person gives it
(`--surface src/api/**`) or a workflow step names.

A worker is told its surfaces; what it changed is read from git after each
attempt, and a change outside them is the attempt's failure, undone in the
task's worktree (never in a person's checkout). So the bound is enforced, not
only asked for. Surfaces narrow; they grant nothing, so a definition a
repository commits may carry them.

Two tasks whose surfaces cannot overlap may run side by side in one
repository without asking anyone. When it cannot be told (a glob this cannot
read, a task without surfaces), they may overlap, and the dispatcher's own
judgment decides.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath

_GLOB = re.compile(r"[*?\[]")
MAX_SURFACES = 20


class SurfaceError(ValueError):
    pass


def check(values: Iterable[str]) -> tuple[str, ...]:
    """Surfaces as given, each relative and inside the project."""
    out = []
    for raw in values:
        value = str(raw).strip()
        if value in (".", "./"):
            value = "**"
        while value.startswith("./"):
            value = value[2:]
        if not value:
            continue
        if value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise SurfaceError(f"a surface stays inside the project: {raw!r}")
        out.append(value.rstrip("/") + ("/**" if value.endswith("/") else ""))
    if len(out) > MAX_SURFACES:
        raise SurfaceError(f"at most {MAX_SURFACES} surfaces")
    return tuple(dict.fromkeys(out))


def _regex(surface: str) -> re.Pattern[str]:
    """A surface as a pattern: `**` crosses directories, `*` and `?` do not, and a
    literal path holds itself and everything under it."""
    out, i = "", 0
    while i < len(surface):
        if surface.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif surface.startswith("**", i):
            out, i = out + ".*", i + 2
        elif surface[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif surface[i] == "?":
            out, i = out + "[^/]", i + 1
        elif surface[i] == "[":
            end = surface.find("]", i)
            if end == -1:
                out, i = out + re.escape("["), i + 1
            else:
                out, i = out + surface[i:end + 1], end + 1
        else:
            out, i = out + re.escape(surface[i]), i + 1
    return re.compile(out + ("(?:/.*)?" if not _GLOB.search(surface) else "") + r"\Z")


def allows(surfaces: Sequence[str], path: str) -> bool:
    """Whether a changed path is inside the surfaces."""
    return any(_regex(s).match(path) for s in surfaces)


def outside(surfaces: Sequence[str], changed: Iterable[str]) -> list[str]:
    return [p for p in changed if not allows(surfaces, p)] if surfaces else []


def _prefix(surface: str) -> str:
    """The literal directory a surface starts with, before any glob."""
    head = _GLOB.split(surface, 1)[0]
    return head if _GLOB.search(surface) is None else head.rsplit("/", 1)[0] if "/" in head else ""


def disjoint(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether two sets of surfaces can never touch the same file. When in doubt, no."""
    if not a or not b:
        return False
    for x in a:
        for y in b:
            px, py = _prefix(x), _prefix(y)
            if not px or not py:
                return False  # a glob from the top could reach anywhere
            if px == py or px.startswith(py + "/") or py.startswith(px + "/"):
                return False
    return True


def overlapping(groups: Mapping[str, Sequence[str]]) -> list[str]:
    """Each pair of surfaces of two groups (a plan's parts) that may touch the same file,
    as `api's src/** and web's src/web/**`; empty when every group is disjoint from the rest."""
    names, out = list(groups), []
    for n, a in enumerate(names):
        for b in names[n + 1:]:
            out += [f"{a}'s {x} and {b}'s {y}" for x in groups[a] for y in groups[b] if not disjoint([x], [y])]
    return out


def brief(surfaces: Sequence[str]) -> str:
    if not surfaces:
        return ""
    return ("Change only files inside these surfaces: " + ", ".join(surfaces) + ". A change outside them is "
            "undone and fails the attempt; if the task needs one, say so and stop.")
