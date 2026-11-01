"""Keep the livespec locks runs current, checked before it is taken.

    python scripts/livespec_check.py newer          # the release on PyPI newer than PINNED, else exit 1
    python scripts/livespec_check.py bump 0.37.0    # PINNED and its comments in the adapter, a CHANGELOG line
    python scripts/livespec_check.py verify         # the pinned release against a real index of this repo

`verify` indexes a copy of this repository with the pinned release and asks the adapter for what a
task gets from it: the index read, a brief, what a change reaches, every tool locks names served, and
`validate_openspec` on a small spec tree. Anything short of that fails. The weekly workflow
(`.github/workflows/livespec-pin.yml`) runs newer, bump, verify and the suite, and opens a pull
request only when all of it passed. Standard library only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "src" / "locks" / "adapters" / "livespec.py"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPI = "https://pypi.org/pypi/livespec/json"


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def pinned(text: str | None = None) -> str:
    found = re.search(r'^PINNED = "([^"]+)"', text if text is not None else ADAPTER.read_text(), re.M)
    if not found:
        raise SystemExit("no PINNED in the livespec adapter")
    return found.group(1)


def latest(fetch: Callable[[str], bytes] | None = None) -> str:
    """The newest livespec release on PyPI."""
    def get(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=30) as reply:  # noqa: S310 - a fixed https URL
            return reply.read()

    return str(json.loads((fetch or get)(PYPI))["info"]["version"])


def newer(current: str, found: str) -> bool:
    return _tuple(found) > _tuple(current)


def bumped(adapter: str, changelog: str, old: str, new: str) -> tuple[str, str]:
    """The adapter with PINNED and the comments that name the checked release moved to `new`, and the
    CHANGELOG with a line under `[Unreleased]`. Refuses an adapter that does not name `old` as such."""
    if pinned(adapter) != old:
        raise SystemExit(f"the adapter pins {pinned(adapter)}, not {old}")
    adapter = adapter.replace(f'PINNED = "{old}"', f'PINNED = "{new}"')
    adapter = adapter.replace(f"Checked against livespec {old}.", f"Checked against livespec {new}.")
    adapter = adapter.replace(f"Verified to exist in {old}.", f"Verified to exist in {new}.")
    line = (f"- **livespec pinned at {new}** (was {old}), checked by `scripts/livespec_check.py verify` against a "
            "real index of this repository: the index read, a brief, a change's reach, every tool locks names "
            "served, and `validate_openspec`.\n")
    head = "## [Unreleased]\n"
    if head not in changelog:
        raise SystemExit("no [Unreleased] section in the CHANGELOG")
    return adapter, changelog.replace(head, head + "\n### Changed\n\n" + line, 1)


def verify() -> list[str]:
    """What the pinned release failed to give a task here; empty when it gave all of it."""
    sys.path.insert(0, str(ROOT / "src"))
    import os

    from locks.adapters.livespec import PINNED, TOOLS, Livespec, LivespecError

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="livespec-check-") as tmp:
        work = Path(tmp)
        copy = work / "repo"
        subprocess.run(["git", "clone", "-q", str(ROOT), str(copy)], check=True)
        env = {**os.environ, "LOCKS_HOME": str(work / "home")}
        adapter = Livespec(env)
        indexed = subprocess.run(["uvx", f"livespec@{PINNED}", "index", str(copy)], capture_output=True, text=True,
                                 timeout=900, check=False)
        if indexed.returncode != 0:
            return [f"livespec {PINNED} index failed: {indexed.stderr.strip()[-300:]}"]
        status = adapter.inspect(copy)
        if not status.present:
            return [f"the index livespec {PINNED} built cannot be read: {status.line()}"]
        if not adapter.brief("Change how isolate.join merges the parts' branches", copy, status).lines:
            problems.append("a brief naming a known symbol came back empty")
        if not adapter.reach(["src/locks/isolate.py"], copy, status):
            problems.append("the reach of a widely used file came back empty")
        try:
            missing = TOOLS - adapter.list_tools(copy, timeout_s=300)
        except LivespecError as exc:
            missing, problems = set(), [*problems, f"its tools could not be listed: {exc}"]
        if missing:
            problems.append(f"it does not serve {', '.join(sorted(missing))}")
        specs = work / "specs"
        (specs / "openspec" / "specs" / "pricing").mkdir(parents=True)
        (specs / "openspec" / "specs" / "pricing" / "spec.md").write_text(
            "# Pricing\n\n## Purpose\nPrices.\n\n## Requirements\n\n### Requirement: Cents\nPrices SHALL be stored "
            "in cents.\n\n#### Scenario: Stored\n- **WHEN** a price is saved\n- **THEN** it is kept in cents\n")
        (specs / "p.py").write_text("PRICE = 100\n")
        for args in (["init", "-q"], ["add", "."], ["-c", "user.email=c@c", "-c", "user.name=c", "commit", "-qm", "s"]):
            subprocess.run(["git", *args], cwd=specs, check=True)
        try:
            found = adapter.validate_openspec(specs, timeout_s=600)
            if not found.get("valid"):
                problems.append(f"validate_openspec found a valid spec tree invalid: {found}")
        except LivespecError as exc:
            problems.append(f"validate_openspec failed: {exc}")
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] == ["newer"]:
        current, found = pinned(), latest()
        if newer(current, found):
            print(found)
            return 0
        print(f"livespec {current} is current", file=sys.stderr)
        return 1
    if len(argv) == 2 and argv[0] == "bump":
        adapter, changelog = bumped(ADAPTER.read_text(), CHANGELOG.read_text(), pinned(), argv[1])
        ADAPTER.write_text(adapter)
        CHANGELOG.write_text(changelog)
        return 0
    if argv[:1] == ["verify"]:
        problems = verify()
        for problem in problems:
            print(f"livespec_check: {problem}", file=sys.stderr)
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
