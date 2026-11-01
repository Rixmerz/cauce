"""The old name, cauce, wherever an install from before the rename still holds it.

Locks was called cauce. A person who had it installed keeps their environment variables,
their data home, their projects' folders, the branches their tasks kept, the blocks their
workers wrote, the manifests their neighbours ship and the commands they typed. Every place
that must still read the old name reads it from here, so the whole compatibility layer is one
file (and `scripts/rename_to_locks.py` leaves it as it is).

Standard library only, and imported by the package itself (`locks/__init__.py`) before
anything else: the PostToolUse fast path goes through it, so it stays small and never raises.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Any

OLD, NEW = "cauce", "locks"

# --- the environment ------------------------------------------------------------------

OLD_ENV, NEW_ENV = "CAUCE_", "LOCKS_"


def adopt_env(environ: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Every `CAUCE_X` without a `LOCKS_X` sets `LOCKS_X`: what a person or an older parent
    process set keeps working. The old one stays, so a child process gets both."""
    environ = os.environ if environ is None else environ
    for key, value in list(environ.items()):
        if key.startswith(OLD_ENV):
            environ.setdefault(NEW_ENV + key[len(OLD_ENV):], value)
    return environ


def mirror(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """A child's environment with every `LOCKS_X` repeated as `CAUCE_X`, and a `CAUCE_X`
    whose `LOCKS_X` was taken away taken away too: a Claude Code session started under it
    may still load an old cauce plugin's hooks, which must see they run in a worker."""
    for key in [k for k in env if k.startswith(OLD_ENV)]:
        if NEW_ENV + key[len(OLD_ENV):] not in env:
            del env[key]
    for key, value in list(env.items()):
        if key.startswith(NEW_ENV):
            env[OLD_ENV + key[len(NEW_ENV):]] = value
    return env


def getenv(env: Mapping[str, str], name: str, default: str = "") -> str:
    """`env[LOCKS_X]`, else what the old `CAUCE_X` holds: for an environment passed in."""
    value = env.get(name)
    if value is None and name.startswith(NEW_ENV):
        value = env.get(OLD_ENV + name[len(NEW_ENV):])
    return default if value is None else value


# --- the data home --------------------------------------------------------------------

OLD_DB, NEW_DB = "cauce.db", "locks.db"
#: Written in the old home when moving it failed: the failure is said once.
STUCK = "rename-failed.txt"


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        yield
        return
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def home(base: Path) -> Path:
    """`<base>/locks`, moved there from `<base>/cauce` the first time it is asked for.

    The move is one rename under a lock, with a link left at the old path so a process of
    the old version still finds its files. When it cannot be made (a read-only parent, a
    file in the way) the old home is used and that is said once, on stderr."""
    new, old = base / NEW, base / OLD
    if new.exists() or new.is_symlink() or not old.is_dir() or old.is_symlink():
        return new
    try:
        with _flock(base / f".{NEW}-rename.lock"):
            if not new.exists():
                os.rename(old, new)
                with contextlib.suppress(OSError):
                    old.symlink_to(NEW, target_is_directory=True)
        rename_database(new)
        return new
    except OSError as exc:
        _say_stuck(old, f"locks: kept the old home {old} ({exc}); move it to {new} by hand to finish the rename")
        return old


def _say_stuck(old: Path, line: str) -> None:
    marker = old / STUCK
    if marker.exists():
        return
    with contextlib.suppress(OSError):
        marker.write_text(line + "\n", encoding="utf-8")
    print(line, file=sys.stderr)


def rename_database(root: Path) -> bool:
    """`cauce.db` becomes `locks.db`, with a link left at the old name, only while no other
    process has it open: leaving the WAL journal proves that (SQLite refuses while another
    connection holds the file), and the exclusive transaction keeps others out of it until
    the link is there. Anything else leaves it as it is, and `database` reads it there."""
    old, new = root / OLD_DB, root / NEW_DB
    if not old.is_file() or old.is_symlink() or new.exists():
        return False
    import sqlite3  # here, not at the top: the PostToolUse fast path imports this module

    try:
        conn = sqlite3.connect(str(old), timeout=0.2, isolation_level=None)
    except sqlite3.Error:  # pragma: no cover - the file opens lazily: a bad one fails at its first statement
        return False
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            return False  # in a rollback journal, an idle connection elsewhere would not refuse
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":  # pragma: no cover
            return False  # SQLite raises when another connection holds it; a build that answers instead
        conn.execute("BEGIN EXCLUSIVE")
        os.rename(old, new)
        with contextlib.suppress(OSError):
            old.symlink_to(NEW_DB)
        conn.execute("COMMIT")
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        conn.close()


def database(root: Path) -> Path:
    """The store in a home: `locks.db`, or the old `cauce.db` when only that one is there and
    another process still holds it (it is renamed the first time nothing does)."""
    new, old = root / NEW_DB, root / OLD_DB
    if new.exists() or not old.is_file() or old.is_symlink():
        return new
    return new if rename_database(root) else old


# --- a project's folder ---------------------------------------------------------------

OLD_FOLDER, NEW_FOLDER = ".cauce", ".locks"


def project_folder(d: Path) -> Path | None:
    """The folder of a project at `d`: `.locks/`, else an old `.cauce/` (read where it is)."""
    for name in (NEW_FOLDER, OLD_FOLDER):
        if (d / name).is_dir():
            return d / name
    return None


def rename_project_folder(d: Path) -> Path | None:
    """`.cauce/` becomes `.locks/` (never two folders), its own ignore file and a line of the
    checkout's `.gitignore` that names it rewritten. None when there was none to rename."""
    old, new = d / OLD_FOLDER, d / NEW_FOLDER
    if not old.is_dir() or old.is_symlink() or new.exists():
        return None
    try:
        os.rename(old, new)
    except OSError:
        return None
    inner = new / ".gitignore"
    with contextlib.suppress(OSError):
        if inner.is_file():
            inner.write_text(inner.read_text(encoding="utf-8").replace(f"{OLD}'s own", f"{NEW}' own"),
                             encoding="utf-8")
    ignore = d / ".gitignore"
    with contextlib.suppress(OSError, UnicodeDecodeError):
        text = ignore.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        changed = [re.sub(rf"^(/?){re.escape(OLD_FOLDER)}(/?)(\s*)$", rf"\1{NEW_FOLDER}\2\3", line) for line in lines]
        if changed != lines:
            ignore.write_text("".join(changed), encoding="utf-8")
    return new


# --- git: kept branches, refs and trailers --------------------------------------------

#: Branches and refs a task of the old name kept, and their new names.
REF_PREFIXES = (("refs/heads/cauce/", "refs/heads/locks/"), ("refs/cauce/", "refs/locks/"))
#: Commit trailers the old name wrote: every reader accepts both.
OLD_TRAILERS = {"Locks-Task": "Cauce-Task", "Locks-Verified": "Cauce-Verified",
                "Locks-Check-Output": "Cauce-Check-Output"}
OLD_UNVERIFIED = "Cauce-Verified: no"


def old_refs(listing: str) -> list[tuple[str, str]]:
    """(old ref, new ref) for every ref of the old name in `git for-each-ref` output (one
    name per line), whose new name is not taken."""
    names = {line.strip() for line in listing.splitlines() if line.strip()}
    moves = []
    for name in sorted(names):
        for before, after in REF_PREFIXES:
            if name.startswith(before):
                target = after + name[len(before):]
                if target not in names:
                    moves.append((name, target))
    return moves


# --- the worker's blocks --------------------------------------------------------------

OLD_RESULT_FENCE, OLD_REPORT_FENCE = "cauce-result", "cauce-report"
OLD_COMPACTION = "cauce-compaction"


# --- neighbours, engram, plugins, the UI and the store --------------------------------

#: The block a neighbour's manifest kept for the old name, and its file beside it: a
#: contract with other repositories, read after the new one and never dropped.
MANIFEST_KEY, MANIFEST_FILE = OLD, f"{OLD}.json"
#: The prefix engram observations were keyed with.
ENGRAM_PREFIX = OLD
#: The plugin and its launcher under their old name.
PLUGIN, LAUNCHER = OLD, OLD
#: The header the UI's token went in.
UI_HEADER = "X-Cauce-Token"
#: Persisted words: a task's `source`, a stop's `by`, a verdict's `checked`, the bundled team.
SOURCE = WHO = CHECKED = TEAM = OLD


#: The store's meta key that says its rows were read into the new name.
UPGRADED = "renamed.from_cauce"
#: What JSON written under the old name holds, and what it reads as now. Only a value that
#: starts with the old name: a branch (`"cauce/task-3"`), a ref, and the words above.
_JSON_WORDS = (('"cauce/', '"locks/'), ('"refs/cauce/', '"refs/locks/'), ('"by": "cauce"', '"by": "locks"'),
               ('"checked": "cauce"', '"checked": "locks"'), ('"source": "cauce"', '"source": "locks"'))
#: The JSON columns that may hold them.
_JSON_COLUMNS = (("events", "data"), ("tasks", "options"), ("workflow_runs", "options"))


def upgrade_store(conn: Any) -> int:
    """Rows written under the old name, read into the new one, in the store itself: a task's
    `source`, and in JSON a kept branch's name, a ref, a stop's `by` and a verdict's
    `checked`. All of it once (the meta key says so), and again whenever an old version
    still running wrote a task since, so nothing it left reads as another's work. Returns
    the rows it changed."""
    done = conn.execute("SELECT 1 FROM meta WHERE key = ?", (UPGRADED,)).fetchone() is not None
    stray = conn.execute("SELECT 1 FROM tasks WHERE source = ? LIMIT 1", (OLD,)).fetchone() is not None
    if done and not stray:
        return 0
    changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed += conn.execute("UPDATE tasks SET source = ? WHERE source = ?", (NEW, OLD)).rowcount
        for table, column in _JSON_COLUMNS:
            if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                continue  # a column the store adds after its schema, not added (yet) on this one
            expr = column
            for before, after in _JSON_WORDS:
                expr = f"replace({expr}, '{before}', '{after}')"
            changed += conn.execute(f"UPDATE {table} SET {column} = {expr} "  # noqa: S608 - names are constants
                                    f"WHERE {column} LIKE '%{OLD}%' AND {column} != {expr}").rowcount
        conn.execute("INSERT INTO meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO NOTHING", (UPGRADED,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed


def say_renamed(root: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Once per home, when run as `cauce` (the console alias, the shim): the line saying
    the command is `locks` now. None every other time."""
    environ = os.environ if environ is None else environ
    invoked = environ.get("LOCKS_INVOKED_AS") or Path(sys.argv[0] if sys.argv else "").name
    if invoked != OLD:
        return None
    marker = root / "said-renamed"
    if marker.exists():
        return None
    with contextlib.suppress(OSError):
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="utf-8")
    return "cauce is now Locks: the command is `locks` (`cauce` keeps working)"
