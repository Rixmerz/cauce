"""A folder of repositories that is one project: a workspace.

Work that spans two or more repositories (an API and the front end that shares
its spec) is one project to the person who does it: one memory, one board, one
lane. A workspace is a folder they declare (`locks workspace add`) holding
those repositories, in it or linked into it. Every repository in it answers to
the folder: its key is the folder's (`repo.key`), a session in it belongs to
the folder's `.locks/` (`project.find`), its notes are the folder's, and a task
that spans the repositories runs in the folder, which holds every checkout in
it while it runs.

Declared in the person's locks home (`workspaces.json`), never in a
repository: a repository must not choose which project it belongs to, nor whose
notes its workers read, by committing a file. Standard library only and no git
call on the read path: `repo.key`, which the hooks call, reads it.

    {"workspaces": [{"folder": "/abs/shop", "members": [{"name": "api", "path": "/abs/shop/api"},
                                                         {"name": "web", "path": "/abs/web"}]}]}

A member's `path` is its checkout's real top; `name` is what it is called in the
folder (its directory there, or the link to it). A note anchored to a member's
file keeps the name in front of the path (`api/src/cart.py`), so two members'
files of the same name are two files.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

FILE = "workspaces.json"
#: Repositories a workspace holds at the least: one is just a project.
MIN_MEMBERS = 2


class WorkspaceError(ValueError):
    """A workspace that cannot be: too few repositories, a member of another one, home or `/`."""


def _file(env: Mapping[str, str] | None = None) -> Path:
    from locks.store import home

    return home(dict(env) if env is not None else None) / FILE


_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def declared(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every workspace the person declared; nothing when the file is absent or unreadable."""
    path = _file(env)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    cached = _cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        found = json.loads(path.read_text(encoding="utf-8")).get("workspaces") or []
    except (OSError, ValueError, AttributeError):
        found = []
    found = [{**w, "members": [m for m in w.get("members") or () if isinstance(m, dict)
                               and isinstance(m.get("path"), str) and isinstance(m.get("name"), str)]}
             for w in found if isinstance(w, dict) and isinstance(w.get("folder"), str)
             and isinstance(w.get("members") or [], list)]
    _cache[str(path)] = (stamp, found)
    return found


def _write(found: list[dict[str, Any]], env: Mapping[str, str] | None = None) -> None:
    path = _file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"workspaces": found}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    _cache.pop(str(path), None)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def of(directory: str | Path | None, env: Mapping[str, str] | None = None) -> Path | None:
    """The workspace folder `directory` belongs to: the folder itself, a path under
    it, or a path in a member linked into it from elsewhere. None outside them."""
    if not directory:
        return None
    try:
        here = Path(directory).resolve()
    except OSError:
        return None
    for w in declared(env):
        folder = Path(w["folder"])
        if _within(here, folder) or any(_within(here, Path(m.get("path", ""))) for m in w.get("members") or ()
                                        if m.get("path")):
            return folder
    return None


def member(top: str | Path | None, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The member whose checkout `top` is (a worktree of it included), with the
    workspace it is in; None outside one."""
    if not top or not declared(env):
        return None
    here = Path(top).resolve()
    tops = [here]
    if (here / ".git").is_file():
        # A worktree (Claude Code's, `git worktree add`): it is its checkout's.
        from locks import repo

        common = repo._git(here, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common and Path(common).name == ".git":
            tops.append(Path(common).parent.resolve())
    for w in declared(env):
        for m in w.get("members") or ():
            if Path(m["path"]) in tops:
                return {**m, "folder": w["folder"]}
    return None


def prefix(top: str | Path | None, env: Mapping[str, str] | None = None) -> str:
    """What a member's file paths carry in front in the workspace's notes (`api/`); "" outside one."""
    found = member(top, env)
    return f"{found['name']}/" if found else ""


def checkouts_of(directory: str | Path | None, env: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    """Where a directory's note paths live: (what they carry in front, the checkout's
    top). A checkout gives itself, named as its workspace names it; a workspace's
    folder gives each of its members; anything else, nothing."""
    from locks import repo

    if not directory:
        return []
    top = repo.toplevel(Path(directory))
    if top is not None:
        return [(prefix(top, env), top)]
    here = Path(directory).resolve()
    entry = next((w for w in declared(env) if Path(w["folder"]) == here), None)
    return [(f"{m['name']}/", Path(m["path"])) for m in (entry or {}).get("members") or ()]


def _checkouts(folder: Path) -> list[tuple[str, Path]]:
    """The checkouts right inside `folder`, a link to one included: (name, real top)."""
    out = []
    for child in sorted(folder.iterdir()) if folder.is_dir() else ():
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / ".git").exists():
            out.append((child.name, child.resolve()))
    return out


def _in_checkout(where: Path) -> bool:
    """`where`, or the nearest folder above it that exists, lies in a git checkout."""
    from locks import repo

    probe = where
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return repo.toplevel(probe) is not None


def add(folder: str | Path, repos: Iterable[str | Path] = (), env: Mapping[str, str] | None = None
        ) -> dict[str, Any]:
    """Declare `folder` a workspace of `repos` (each one outside it linked in under
    its own name), or of the checkouts right inside it when none is named; the
    folder is made when it is not there. A workspace's folder holds only its own
    repositories: every one under it belongs to it. Adding to a workspace that
    exists adds its new members. Everything is checked before anything is made."""
    from locks import repo

    where = Path(folder).expanduser().resolve()
    if where == where.parent or where == Path.home().resolve():
        raise WorkspaceError(f"{where} cannot be a workspace: the home directory and / are never a project")
    if _in_checkout(where):
        raise WorkspaceError(f"{where} is in a git checkout: a workspace is a folder that holds checkouts, "
                             "never one inside a checkout")
    current = next((w for w in declared(env) if Path(w["folder"]) == where), None)
    members = {m["path"]: m["name"] for m in (current or {}).get("members") or ()}
    inside = {str(top): name for name, top in _checkouts(where)}
    links: list[tuple[Path, Path]] = []
    named = list(dict.fromkeys(str(t.resolve()) for t in (repo.toplevel(Path(r).expanduser()) or Path(r)
                                                            for r in repos)))
    for given in named:
        top = Path(given)
        if repo.toplevel(top) is None:
            raise WorkspaceError(f"{given} is not in a git checkout")
        if given in members:
            continue
        if _within(top, where):
            members[given] = top.relative_to(where).as_posix()
            continue
        if given in inside:
            members[given] = inside[given]
            continue
        link = where / top.name
        if link.exists() or link.is_symlink() or any(link == planned for planned, _ in links):
            raise WorkspaceError(f"{link} is already there, or named twice: rename one of the repositories")
        links.append((link, top))
        members[given] = top.name
    if not named and not current:
        members = {path: name for path, name in inside.items()}
    others = sorted(name for path, name in inside.items() if path not in members)
    if others:
        raise WorkspaceError(f"{where} also holds {', '.join(others)}: a workspace's folder holds only its own "
                             "repositories (name them all with --with, or make a new folder for these)")
    if len(members) < MIN_MEMBERS:
        raise WorkspaceError(f"{where} would hold {len(members)} repository(ies): a workspace holds {MIN_MEMBERS} "
                             "or more (name them with --with, or put them in the folder)")
    found = [w for w in declared(env) if Path(w["folder"]) != where]
    for w in found:
        taken = {m["path"] for m in w.get("members") or ()} & set(members)
        if taken or _within(where, Path(w["folder"])) or _within(Path(w["folder"]), where):
            raise WorkspaceError(f"{where} overlaps the workspace {w['folder']}: a repository is in one workspace")
    where.mkdir(parents=True, exist_ok=True)
    for link, top in links:
        link.symlink_to(top, target_is_directory=True)
    entry = {"folder": str(where), "members": [{"name": name, "path": path} for path, name in members.items()]}
    _write([*found, entry], env)
    return entry


def remove(folder: str | Path, env: Mapping[str, str] | None = None) -> bool:
    """Forget a workspace: its repositories are projects of their own again. Its
    folder, links and notes stay where they are."""
    where = Path(folder).expanduser().resolve()
    found = declared(env)
    kept = [w for w in found if Path(w["folder"]) != where]
    if len(kept) == len(found):
        return False
    _write(kept, env)
    return True
