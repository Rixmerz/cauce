"""Move locks to a new version in every file that names it, at once, and close its CHANGELOG section.

The version lives in five places (`src/locks/__init__.py`, `pyproject.toml`, `uv.lock`, the plugin's
manifest and the development marketplace) and the CHANGELOG's `[Unreleased]` section becomes the
new version's, dated today, with an empty `[Unreleased]` above it. Nothing is written unless every
file holds the current version where it is expected and `[Unreleased]` says something: a release is
never cut half-bumped or without its notes. The release workflow tags it once CI passes on main.

    python scripts/bump.py 0.64.0          # writes
    python scripts/bump.py 0.64.0 --check  # says what it would change, writes nothing
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def current(root: Path = ROOT) -> str:
    found = re.search(r'^__version__ = "([^"]+)"', (root / "src/locks/__init__.py").read_text(), re.MULTILINE)
    if not found:
        raise SystemExit("src/locks/__init__.py declares no __version__")
    return found.group(1)


def _one(text: str, pattern: str, new: str, where: str) -> str:
    """`text` with the one match of `pattern` (its first group the version) set to `new`."""
    found = list(re.finditer(pattern, text, re.MULTILINE))
    if len(found) != 1:
        raise SystemExit(f"{where}: expected one version line, found {len(found)}")
    m = found[0]
    return text[:m.start(1)] + new + text[m.end(1):]


def changelog(text: str, new: str, today: str) -> str:
    """The CHANGELOG with `[Unreleased]`'s entries moved under `[new] - today`."""
    head = re.search(r"^## \[Unreleased\][^\n]*\n", text, re.MULTILINE)
    if not head:
        raise SystemExit("CHANGELOG.md has no [Unreleased] section")
    rest = text[head.end():]
    after = re.search(r"^## \[", rest, re.MULTILINE)
    body = rest[:after.start()] if after else rest
    if not body.strip():
        raise SystemExit("CHANGELOG.md's [Unreleased] section is empty: write the release's notes first")
    if re.search(rf"^## \[{re.escape(new)}\]", text, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md already has a section for {new}")
    return text[:head.end()] + f"\n## [{new}] - {today}\n\n" + body.strip("\n") + "\n\n" + (rest[after.start():]
                                                                                       if after else "")


def plan(new: str, root: Path = ROOT, today: str | None = None) -> dict[Path, str]:
    """Every file's new text; SystemExit when one is not as expected."""
    if not _VERSION.match(new):
        raise SystemExit(f"{new!r} is no version (X.Y.Z)")
    old = current(root)
    if tuple(map(int, new.split("."))) <= tuple(map(int, old.split("."))):
        raise SystemExit(f"{new} is not after the current {old}")
    esc = re.escape(old)
    out: dict[Path, str] = {}
    init = root / "src/locks/__init__.py"
    out[init] = _one(init.read_text(), rf'^__version__ = "({esc})"', new, str(init))
    pyproject = root / "pyproject.toml"
    out[pyproject] = _one(pyproject.read_text(), rf'^version = "({esc})"', new, str(pyproject))
    lock = root / "uv.lock"
    out[lock] = _one(lock.read_text(), rf'name = "locks"\nversion = "({esc})"', new, str(lock))
    for name in ("plugin.json", "marketplace.json"):
        path = root / ".claude-plugin" / name
        text = path.read_text()
        out[path] = _one(text, rf'^\s*"version": "({esc})"', new, str(path))
        json.loads(out[path])
    log = root / "CHANGELOG.md"
    out[log] = changelog(log.read_text(), new, today or date.today().isoformat())
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    changes = plan(argv[0])
    for path in changes:
        print(("would change " if "--check" in argv else "changed ") + str(path.relative_to(ROOT)))
    if "--check" not in argv:
        for path, text in changes.items():
            path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
