"""The Node a project declares, put first on a worker's PATH.

A worker inherits the PATH of whoever ran `cauce`, and with it whatever Node
was the default there. A project that needs another one sends the worker
hunting: `source nvm.sh`, `nvm use`, `~/.nvm/versions/node/v22.x/bin/node`,
`/opt/homebrew/bin/node` — each a different command, each refused, each a
new `--allow` to pass. So cauce reads the version the project declares —
`.nvmrc`, `.node-version`, or `engines.node` in `package.json` — finds it
among the versions nvm installed, and puts its `bin` first on the PATH of the
worker and of its `--verify` check. Nothing is installed or switched.

Standard library only.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

VERSION_FILES = (".nvmrc", ".node-version")
_PARTS = re.compile(r"v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?")


@dataclass(frozen=True)
class Node:
    version: str
    bin: Path
    #: Where the wanted version came from, e.g. `.nvmrc`.
    source: str


def wanted(workdir: Path) -> tuple[str, str] | None:
    """(the version spec, the file it came from), looking in `workdir` and its
    parents up to the top of its checkout."""
    for d in (workdir, *workdir.parents):
        for name in VERSION_FILES:
            f = d / name
            if f.is_file():
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text.splitlines()[0].strip(), name
        manifest = d / "package.json"
        if manifest.is_file():
            try:
                engines = json.loads(manifest.read_text(encoding="utf-8")).get("engines") or {}
            except (OSError, ValueError, AttributeError):
                engines = {}
            if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                return engines["node"].strip(), "package.json engines.node"
        if (d / ".git").exists():
            break
    return None


def installed(env: Mapping[str, str] | None = None) -> list[tuple[tuple[int, ...], Path]]:
    """The Node versions nvm installed, oldest first, with their `bin`."""
    env = os.environ if env is None else env
    root = Path(env.get("NVM_DIR") or Path(env.get("HOME") or Path.home()) / ".nvm") / "versions" / "node"
    found = []
    try:
        for d in root.iterdir():
            version = _version(d.name)
            if version and len(version) == 3 and (d / "bin" / "node").exists():
                found.append((version, d / "bin"))
    except OSError:
        return []
    return sorted(found)


def node_bin(workdir: Path, env: Mapping[str, str] | None = None) -> Node | None:
    """The installed Node that satisfies what the project declares, the newest
    that does. None when it declares nothing, or nothing installed fits."""
    found = wanted(workdir)
    if found is not None:
        specs, source = [found[0]], found[1]
    else:
        # A folder that holds several projects declares nothing itself: the
        # Node that fits every project in it does.
        from cauce.launch import nested_checkouts

        inner = [w for w in (wanted(sub) for sub in nested_checkouts(workdir)) if w is not None]
        if not inner:
            return None
        specs, source = [w[0] for w in inner], "the projects in this folder"
    fits = [(v, b) for v, b in installed(env) if all(satisfies(v, spec) for spec in specs)]
    if not fits:
        return None
    version, binary = fits[-1]
    return Node(".".join(map(str, version)), binary, source)


def env_for(workdir: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{"PATH": ...}` with the project's Node first, or `{}`."""
    env = os.environ if env is None else env
    node = node_bin(workdir, env)
    if node is None:
        return {}
    return {"PATH": os.pathsep.join([str(node.bin), env.get("PATH", "")])}


def satisfies(version: tuple[int, ...], spec: str) -> bool:
    """Whether a version meets a spec as `.nvmrc` and `engines` write them: `22`,
    `v22.23.2`, `22.x`, `^22.12.0`, `~22.22`, `>=22.22.3`, `>=22 <24`, `a || b`.
    A name (`lts/*`, `node`) matches nothing: cauce does not guess."""
    spec = spec.strip()
    if "||" in spec:
        return any(satisfies(version, part) for part in spec.split("||"))
    terms = spec.split()
    return bool(terms) and all(_term(version, term) for term in terms)


def _term(version: tuple[int, ...], term: str) -> bool:
    op = re.match(r"^(>=|<=|>|<|\^|~|=)?", term).group(0)
    parts = _version(term[len(op):], partial=True)
    if parts is None:
        return False
    padded = parts + (0,) * (3 - len(parts))
    if op == ">=":
        return version >= padded
    if op == ">":
        return version > padded
    if op == "<=":
        return version <= padded
    if op == "<":
        return version < padded
    if op == "^":
        return version[0] == parts[0] and version >= padded
    if op == "~":
        return version[:2] == padded[:2] and version >= padded
    return version[:len(parts)] == parts  # `22`, `22.23`, `22.23.2`, `22.x`


def _version(text: str, *, partial: bool = False) -> tuple[int, ...] | None:
    found = _PARTS.fullmatch(text.strip())
    if found is None:
        return None
    parts = []
    for group in found.groups():
        if group is None or group in ("x", "*"):
            break
        parts.append(int(group))
    if not partial and len(parts) < 3:
        return None
    return tuple(parts) or None
