"""A folder of repositories that is one project: a workspace.

Work that spans two or more repositories (an API and the front end that shares
its spec) is one project to the person who does it: one memory, one board, one
lane. A workspace is a folder they declare (`cauce workspace add`) holding
those repositories, in it or linked into it. Every repository in it answers to
the folder: its key is the folder's (`repo.key`), a session in it belongs to
the folder's `.cauce/` (`project.find`), its notes are the folder's, and a task
that spans the repositories runs in the folder, which holds every checkout in
it while it runs.

Declared in the person's cauce home (`workspaces.json`), never in a
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
    from cauce.store import home

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
    found = [w for w in found if isinstance(w, dict) and isinstance(w.get("folder"), str)]
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
    """The member whose checkout `top` is, with the workspace it is in; None outside one."""
    if not top:
        return None
    here = Path(top).resolve()
    for w in declared(env):
        for m in w.get("members") or ():
            if m.get("path") and Path(m["path"]) == here:
                return {**m, "folder": w["folder"]}
    return None


def prefix(top: str | Path | None, env: Mapping[str, str] | None = None) -> str:
    """What a member's file paths carry in front in the workspace's notes (`api/`); "" outside one."""
    found = member(top, env)
    return f"{found['name']}/" if found else ""


def _checkouts(folder: Path) -> list[tuple[str, Path]]:
    """The checkouts right inside `folder`, a link to one included: (name, real top)."""
    out = []
    for child in sorted(folder.iterdir()) if folder.is_dir() else ():
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / ".git").exists():
            out.append((child.name, child.resolve()))
    return out


def add(folder: str | Path, repos: Iterable[str | Path] = (), env: Mapping[str, str] | None = None
        ) -> dict[str, Any]:
    """Declare `folder` a workspace of the checkouts in it and of `repos`, each one
    outside it linked in under its own name (the folder is made when it is not
    there). Adding to a workspace that exists adds its new members."""
    from cauce import repo

    where = Path(folder).expanduser().resolve()
    if where == where.parent or where == Path.home().resolve():
        raise WorkspaceError(f"{where} cannot be a workspace: the home directory and / are never a project")
    if (where / ".git").exists():
        raise WorkspaceError(f"{where} is itself a checkout: a workspace is a folder that holds checkouts")
    links: list[tuple[Path, Path]] = []
    for given in repos:
        top = repo.toplevel(Path(given).expanduser())
        if top is None:
            raise WorkspaceError(f"{given} is not in a git checkout")
        top = top.resolve()
        if _within(top, where):
            continue
        link = where / top.name
        if link.exists() or link.is_symlink():
            if link.resolve() != top:
                raise WorkspaceError(f"{link} is already there and is not {top}: rename one of them")
            continue
        links.append((link, top))
    # Everything is checked before anything is made: a refused workspace leaves no link behind.
    members = [{"name": name, "path": str(top)} for name, top in _checkouts(where)]
    members += [{"name": link.name, "path": str(top)} for link, top in links]
    if len(members) < MIN_MEMBERS:
        raise WorkspaceError(f"{where} holds {len(members)} repository(ies): a workspace holds {MIN_MEMBERS} or more "
                             "(name them with --with, or put them in the folder)")
    found = [w for w in declared(env) if Path(w["folder"]) != where]
    for w in found:
        taken = {m["path"] for m in w.get("members") or ()} & {m["path"] for m in members}
        if taken or _within(where, Path(w["folder"])) or _within(Path(w["folder"]), where):
            raise WorkspaceError(f"{where} overlaps the workspace {w['folder']}: a repository is in one workspace")
    where.mkdir(parents=True, exist_ok=True)
    for link, top in links:
        link.symlink_to(top, target_is_directory=True)
    entry = {"folder": str(where), "members": members}
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
