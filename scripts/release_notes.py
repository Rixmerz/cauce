"""What a release says and whether it is due, read from the repository's own files.

`version` prints the version `pyproject.toml` declares; `notes VERSION` prints that version's
section of `CHANGELOG.md` (from its `## [VERSION]` heading to the next `## [` heading, the
heading itself left out), and fails when the CHANGELOG has none: a release is never cut without
its notes. The release workflow (`.github/workflows/release.yml`) runs it; stdlib only.

    python scripts/release_notes.py version
    python scripts/release_notes.py notes 0.63.0
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version(pyproject: str) -> str:
    """The `version` of the `[project]` table."""
    table = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", pyproject, re.MULTILINE | re.DOTALL)
    found = re.search(r'^version\s*=\s*"([^"]+)"', table.group(1) if table else "", re.MULTILINE)
    if not found:
        raise SystemExit("pyproject.toml declares no [project] version")
    return found.group(1)


def notes(changelog: str, wanted: str) -> str:
    """The CHANGELOG section of `wanted`, without its heading; SystemExit when it has none."""
    head = re.compile(rf"^## \[{re.escape(wanted)}\][^\n]*$", re.MULTILINE)
    found = head.search(changelog)
    if not found:
        raise SystemExit(f"CHANGELOG.md has no section for {wanted}")
    rest = changelog[found.end():]
    after = re.search(r"^## \[", rest, re.MULTILINE)
    body = (rest[:after.start()] if after else rest).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md's section for {wanted} is empty")
    return body


def main(argv: list[str]) -> int:
    if argv[:1] == ["version"]:
        print(version((ROOT / "pyproject.toml").read_text(encoding="utf-8")))
        return 0
    if len(argv) == 2 and argv[0] == "notes":
        print(notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), argv[1]))
        return 0
    print("\n".join(__doc__.strip().splitlines()[-2:]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
