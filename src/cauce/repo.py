"""Which repository a directory belongs to, as one stable key.

Two clones of the same project are one project to the memory: a dead end found
in one is a dead end in the other. Two unrelated checkouts that happen to share
a directory name are two. So the key is the normalized `origin` URL when there
is one, and the checkout's absolute top-level path when there is not. A
repository in a workspace the person declared answers to the workspace's
folder instead (`cauce.workspaces`): several repositories, one project.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SCP = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>.+)$")


def _git(directory: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(directory), capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


#: What `git` said of a directory's checkout and of a checkout's key, kept for this process: a hook
#: asked it a dozen times per directory, one process each. Each answer is checked before it is used
#: again (`_still_top`, `_stamp`), with a few `stat` calls instead of a process.
_TOPS: dict[str, Path] = {}
_KEYS: dict[str, tuple[int, str]] = {}


def _still_top(directory: Path, top: Path) -> bool:
    """Whether `top` is still the checkout `directory` is in: both there, and no `.git` between
    them (a repository made inside since, a worktree)."""
    if not top.is_dir() or not (top / ".git").exists():
        return False
    here = directory
    while here != top:
        if (here / ".git").exists():
            return False
        if here.parent == here:
            return False
        here = here.parent
    return True


def toplevel(directory: Path) -> Path | None:
    directory = Path(directory)
    where = str(directory.absolute())
    if (seen := _TOPS.get(where)) is not None and _still_top(directory.absolute(), seen):
        return seen
    out = _git(directory, "rev-parse", "--show-toplevel")
    if not out:
        _TOPS.pop(where, None)
        return None
    _TOPS[where] = Path(out)
    return Path(out)


def normalize_remote(url: str) -> str:
    """`git@github.com:o/r.git`, `https://github.com/o/r` and `ssh://git@github.com/o/r.git`
    are one repository: `github.com/o/r`."""
    url = url.strip()
    if "://" in url:
        rest = url.split("://", 1)[1]
        rest = rest.split("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
        host, _, path = rest.partition("/")
    else:
        match = _SCP.match(url)
        if not match:
            return url
        host, path = match["host"], match["path"]
    host = host.split(":", 1)[0].lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}"


def _stamp(top: Path) -> int:
    """When the checkout's `.git` last changed: git rewrites its config by renaming into it."""
    try:
        return (top / ".git").stat().st_mtime_ns
    except OSError:
        return -1


def _top_key(top: Path) -> str:
    stamp = _stamp(top)
    if (seen := _KEYS.get(str(top))) is not None and seen[0] == stamp and stamp != -1:
        return seen[1]
    remote = _git(top, "remote", "get-url", "origin")
    found = normalize_remote(remote) if remote else str(top.resolve())
    _KEYS[str(top)] = (stamp, found)
    return found


def own_key(directory: Path | str | None) -> str | None:
    """The key of the checkout itself, whatever workspace holds it."""
    if directory is None:
        return None
    directory = Path(directory)
    if not directory.is_dir():
        return None
    top = toplevel(directory)
    return _top_key(top) if top is not None else str(directory.resolve())


def key(directory: Path | str | None) -> str | None:
    """The project a directory belongs to: the workspace folder the person declared
    that holds it (`cauce.workspaces`), else its own checkout's key."""
    from cauce import workspaces

    if directory is None or not Path(directory).is_dir():
        return None
    folder = workspaces.of(directory)
    if folder is not None:
        return str(folder)
    top = toplevel(Path(directory))
    if top is None:
        return str(Path(directory).resolve())
    if (top / ".git").is_file() and workspaces.declared():
        # A worktree of a member (a cauce worker's) belongs where its checkout does.
        common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common and Path(common).name == ".git" and (folder := workspaces.of(Path(common).parent)):
            return str(folder)
    return _top_key(top)
