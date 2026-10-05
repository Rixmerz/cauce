"""A project's `.cauce/` folder: whether it is enrolled, and what its sessions are called.

A repository is enrolled when it has a `.cauce/` folder — at the top of the
checkout, or in the directory a session runs in. Only enrolled projects, and
the sessions that run in them, appear in the UI: having the plugin on while
chatting somewhere is not using cauce there. cauce enrolls a project itself
the first time work is queued or run in it (`++`, `cauce queue add`, `cauce
run`), and `cauce init` does it by hand. The folder ignores itself in git, so
enrolling never shows up in anyone's diff.

`.cauce/sessions.json` names each session:

    {"<session id>": {"name": "...", "haiku_name": "...", "prompts": 12, "named_at": "..."}}

Haiku writes `name` and keeps a copy in `haiku_name`. A person may change
`name` by hand; once it differs from `haiku_name`, it is theirs, and Haiku never
touches that session's entry again.

Standard library only, no git call: the hooks and the UI read this for every
session they list.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

FOLDER = ".cauce"
NAMES = "sessions.json"
#: Prompts a session takes before Haiku looks at its name again.
RENAME_EVERY = 10


def find(cwd: str | Path | None) -> Path | None:
    """The `.cauce/` folder a session in `cwd` belongs to: in `cwd` itself, or in a
    parent up to and including the top of its git checkout."""
    if not cwd:
        return None
    here = Path(cwd)
    for d in (here, *here.parents):
        if (d / FOLDER).is_dir():
            return d / FOLDER
        if (d / ".git").exists():
            return None
    return None


def enroll(cwd: str | Path) -> Path:
    """Create the `.cauce/` folder at the top of the checkout `cwd` is in (or in
    `cwd` when it is no checkout), unless one is already found. Returns it."""
    found = find(cwd)
    if found is not None:
        return found
    here = Path(cwd).resolve()
    top = next((d for d in (here, *here.parents) if (d / ".git").exists()), here)
    folder = top / FOLDER
    folder.mkdir(exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# cauce's own state for this project; never committed.\n*\n", encoding="utf-8")
    return folder


def names(folder: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((folder / NAMES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def name_of(cwd: str | Path | None, session_id: str) -> dict[str, Any] | None:
    """{"name", "by"} for a session, `by` being "haiku" or "you"; None when it has none."""
    folder = find(cwd)
    entry = names(folder).get(session_id) if folder else None
    if not entry or not str(entry.get("name") or "").strip():
        return None
    return {"name": str(entry["name"]).strip(), "by": "you" if person_named(entry) else "haiku"}


def person_named(entry: dict[str, Any] | None) -> bool:
    return bool(entry) and entry.get("name") != entry.get("haiku_name")


def wants_name(entry: dict[str, Any] | None, prompts: int) -> bool:
    """Whether Haiku should (re)name a session: never one a person named; a new
    one after its first prompt; a named one every `RENAME_EVERY` prompts."""
    if prompts < 1 or person_named(entry):
        return False
    return entry is None or prompts - int(entry.get("prompts") or 0) >= RENAME_EVERY


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    with open(folder / f"{NAMES}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def set_haiku_name(folder: Path, session_id: str, name: str, prompts: int, stamp: str) -> bool:
    """Write Haiku's name, unless a person renamed the session meanwhile. True if written."""
    with _locked(folder):
        current = names(folder)
        if person_named(current.get(session_id)):
            return False
        current[session_id] = {"name": name, "haiku_name": name, "prompts": prompts, "named_at": stamp}
        tmp = folder / f"{NAMES}.tmp"
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(folder / NAMES)
    return True
