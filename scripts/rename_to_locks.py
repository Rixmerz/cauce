#!/usr/bin/env python3
"""Rename the product from its old name to Locks, deterministically and idempotently.

    python scripts/rename_to_locks.py           # apply: move paths, rewrite text, print a summary
    python scripts/rename_to_locks.py --check   # exit 1 and list what is left un-renamed

Locks is a canal lock: work climbs a ladder of model cells one level at a time, each gate
opening only on evidence. The old name stays only where something outside this tree still
says it (a person's environment, an existing install, a neighbour's manifest, a kept branch,
history): that is the compatibility layer, written by hand, and every place it may appear is
allow-listed below with why.

Standard library only. Paths come from `git ls-files`, so ignored and untracked files are never
touched; a file that is not UTF-8 text (an image, an archive) is left as it is.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

OLD, NEW = "cauce", "locks"

#: A line holding this marker is never rewritten and never reported: it names the old name on
#: purpose (the compatibility layer). Written in any comment syntax (`#`, `//`, `<!-- -->`).
MARKER = "legacy-name"

#: The rewrite rules, most specific first. Each is (name, pattern, replacement); every match of
#: one rule is counted under its name, and a later rule only sees what earlier ones left.
RULES: list[tuple[str, re.Pattern[str], Any]] = [
    ("import (from/import cauce)", re.compile(r"\b(from cauce(?=\.\w| import\b)|import cauce(?![\w'.-]))"),
     lambda m: m.group(1).replace(OLD, NEW)),
    ("module path (cauce.x)", re.compile(r"\bcauce(?=\.[a-z_])"), "locks"),
    ("environment (CAUCE_)", re.compile(r"\bCAUCE_"), "LOCKS_"),
    ("plugin command (/cauce:)", re.compile(r"/cauce:"), "/locks:"),
    ("mcp tool (mcp__cauce__)", re.compile(r"mcp__cauce__"), "mcp__locks__"),
    ("project folder (.cauce)", re.compile(r"(?<![\w.])\.cauce\b"), ".locks"),
    ("branch and ref (cauce/…)", re.compile(r"\bcauce/(?=task-|foreign/|merge-|aside|$|['\"`\s)])"), "locks/"),
    ("protocol fence (cauce-…)", re.compile(r"\bcauce-(?=result|report|compaction|split)"), "locks-"),
    ("trailer (Cauce-…)", re.compile(r"\bCauce-(?=Task|Verified|Check-Output)"), "Locks-"),
    ("ui header (X-Cauce-Token)", re.compile(r"X-Cauce-Token"), "X-Locks-Token"),
    ("command (cauce <verb>)", re.compile(r"\bcauce(?= [a-z][a-z-]+\b)"), "locks"),
    ("possessive (cauce's)", re.compile(r"\bcauce's\b"), "locks'"),
    ("Capitalized (Cauce)", re.compile(r"Cauce"), "Locks"),
    ("upper case (CAUCE)", re.compile(r"CAUCE"), "LOCKS"),
    ("prose and identifiers (cauce)", re.compile(r"cauce"), "locks"),
]

#: Text kept as it is wherever it appears, with why.
KEEP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Rixmerz/cauce\b"),
     "the GitHub repository's own name: renaming the repository is the owner's, and GitHub redirects once it is"),
]

#: Whole files never rewritten (and never reported), with why.
KEEP_FILES: dict[str, str] = {
    "scripts/rename_to_locks.py": "this script: it names the old name to find it",
    "tests/test_rename_script.py": "the script's tests: they build trees holding the old name",
    "bin/cauce": "the old command, kept as a shim that execs bin/locks and says so",
    "src/locks/legacy.py": "the compatibility layer: every old name an existing install still holds",
    "tests/test_legacy_names.py": "the compatibility layer's tests: they write what an old install left",
}

#: Paths whose old name is kept on purpose (the shim above).
KEEP_PATHS = {"bin/cauce"}

#: CHANGELOG.md: only what is not released yet is rewritten; released entries keep the old name.
CHANGELOG = "CHANGELOG.md"
RENAME_NOTE = ("> The project was named cauce until it was renamed Locks; the entries of released versions "
               "below keep the old name. <!-- legacy-name: history -->")

#: uv.lock: only the editable package's own name line.
UV_LOCK = "uv.lock"

#: The old command, kept: it hands every argument to bin/locks and says, once, that it is Locks now.
SHIM = """#!/usr/bin/env bash
# The old name of bin/locks, kept so a link or a script that runs `cauce` keeps working.
# Locks says once (per cauce home) that the command is `locks` now.
set -euo pipefail
SRC="${BASH_SOURCE[0]}"
while [ -L "${SRC}" ]; do
  DIR="$(cd "$(dirname "${SRC}")" && pwd)"
  SRC="$(readlink "${SRC}")"
  [[ "${SRC}" == /* ]] || SRC="${DIR}/${SRC}"
done
export LOCKS_INVOKED_AS=cauce
exec "$(cd "$(dirname "${SRC}")" && pwd)/locks" "$@"
"""

#: pyproject.toml keeps the old console script as an alias.
CONSOLE_ALIAS = 'cauce = "locks.cli:main"  # legacy-name: the old command keeps working'


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def tracked(root: Path) -> list[str]:
    return [p for p in git(root, "ls-files", "-z").split("\0") if p]


def rewrite_line(line: str, counts: Counter[str]) -> str:
    if MARKER in line or OLD not in line.lower():
        return line
    kept: list[str] = []

    def hold(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\0{len(kept) - 1}\0"

    for pattern, _why in KEEP_PATTERNS:
        line = pattern.sub(hold, line)
    for name, pattern, replacement in RULES:
        line, n = pattern.subn(replacement, line)
        if n:
            counts[name] += n
    return re.sub(r"\0(\d+)\0", lambda m: kept[int(m.group(1))], line)


def rewrite_text(path: str, text: str, counts: Counter[str]) -> str:
    lines = text.splitlines(keepends=True)
    if path == UV_LOCK:
        return "".join(NEW.join(line.split(OLD)) if line.strip() == f'name = "{OLD}"' else line for line in lines)
    out, released = [], False
    for line in lines:
        if path == CHANGELOG and re.match(r"## \[\d", line):
            released = True
        out.append(line if released else rewrite_line(line, counts))
    text = "".join(out)
    if path == CHANGELOG and RENAME_NOTE not in text:
        text = text.replace("# Changelog\n", f"# Changelog\n\n{RENAME_NOTE}\n", 1)
    if path == "pyproject.toml" and CONSOLE_ALIAS not in text:
        text = text.replace('locks = "locks.cli:main"\n', f'locks = "locks.cli:main"\n{CONSOLE_ALIAS}\n', 1)
    return text


def left_in(path: str, text: str) -> list[tuple[int, str]]:
    """The lines of a file that still name the old name and no rule allows."""
    if path in KEEP_FILES:
        return []
    found, released = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if path == CHANGELOG and re.match(r"## \[\d", line):
            released = True
        if released or MARKER in line:
            continue
        rest = line
        for pattern, _why in KEEP_PATTERNS:
            rest = pattern.sub("", rest)
        if OLD in rest.lower():
            found.append((number, line.strip()))
    return found


def read(root: Path, path: str) -> str | None:
    try:
        return (root / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        return None


def new_path(path: str) -> str:
    return path.replace(OLD, NEW).replace(OLD.capitalize(), NEW.capitalize())


def move_paths(root: Path) -> list[tuple[str, str]]:
    moved = []
    if (root / "src" / OLD).is_dir() and not (root / "src" / NEW).exists():
        git(root, "mv", f"src/{OLD}", f"src/{NEW}")
        moved.append((f"src/{OLD}/", f"src/{NEW}/"))
    for path in tracked(root):
        if OLD not in path.lower():
            continue
        target = new_path(path)
        if (root / target).exists():
            continue
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", path, target)
        moved.append((path, target))
    shim = root / "bin" / OLD
    if (root / "bin" / NEW).exists() and (not shim.exists() or shim.read_text() != SHIM):
        shim.write_text(SHIM)
        shim.chmod(0o755)
        git(root, "add", f"bin/{OLD}")
        moved.append(("(new)", f"bin/{OLD}"))
    return moved


def apply(root: Path) -> int:
    moved = move_paths(root)
    counts: Counter[str] = Counter()
    changed = []
    for path in tracked(root):
        if path in KEEP_FILES:
            continue
        text = read(root, path)
        if text is None:
            continue
        new = rewrite_text(path, text, counts)
        if new != text:
            (root / path).write_text(new, encoding="utf-8")
            changed.append(path)
    print(f"moved {len(moved)} path(s)")
    for old, new in moved:
        print(f"  {old} -> {new}")
    print(f"rewrote {len(changed)} file(s), {sum(counts.values())} replacement(s)")
    for name, _pattern, _replacement in RULES:
        if counts[name]:
            print(f"  {counts[name]:6d}  {name}")
    return check(root, quiet=True)


def check(root: Path, *, quiet: bool = False) -> int:
    left = [(p, 0, "path") for p in tracked(root) if OLD in p.lower() and p not in KEEP_PATHS]
    for path in tracked(root):
        text = read(root, path)
        if text is not None:
            left += [(path, n, line) for n, line in left_in(path, text)]
    if not left:
        if not quiet:
            print(f"no un-renamed '{OLD}' left outside the allow-list")
        return 0
    print(f"{len(left)} un-renamed occurrence(s) of '{OLD}':")
    for path, number, line in left:
        print(f"  {path}:{number}: {line[:160]}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="list what is left; exit 1 when anything is")
    parser.add_argument("--root", default=None, help="the repository (default: this script's)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    return check(root) if args.check else apply(root)


if __name__ == "__main__":
    sys.exit(main())
