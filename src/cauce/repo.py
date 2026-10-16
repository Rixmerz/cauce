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


def toplevel(directory: Path) -> Path | None:
    out = _git(directory, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


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


def own_key(directory: Path | str | None) -> str | None:
    """The key of the checkout itself, whatever workspace holds it."""
    if directory is None:
        return None
    directory = Path(directory)
    if not directory.is_dir():
        return None
    top = toplevel(directory)
    if top is None:
        return str(directory.resolve())
    remote = _git(top, "remote", "get-url", "origin")
    return normalize_remote(remote) if remote else str(top.resolve())


def key(directory: Path | str | None) -> str | None:
    """The project a directory belongs to: the workspace folder the person declared
    that holds it (`cauce.workspaces`), else its own checkout's key."""
    from cauce import workspaces

    if directory is None or not Path(directory).is_dir():
        return None
    folder = workspaces.of(directory)
    if folder is None and workspaces.declared():
        # A worktree of a member (a cauce worker's) belongs where its checkout does.
        common = _git(Path(directory), "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common and Path(common).name == ".git":
            folder = workspaces.of(Path(common).parent)
    return str(folder) if folder is not None else own_key(directory)
