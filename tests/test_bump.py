"""A version moves in every file that names it at once, and its CHANGELOG section is closed."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bump", ROOT / "scripts" / "bump.py")
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


def _copy(tmp_path: Path, unreleased: str = "### Added\n\n- a thing\n") -> Path:
    for rel in ("src/locks/__init__.py", "pyproject.toml", "uv.lock", ".claude-plugin/plugin.json",
                ".claude-plugin/marketplace.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    old = bump.current(ROOT)
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## [Unreleased]\n\n{unreleased}\n## [{old}] - x\n\n- b\n")
    return tmp_path


def test_every_file_moves_to_the_new_version_and_the_notes_get_its_heading(tmp_path):
    root = _copy(tmp_path)
    old = bump.current(root)
    major, minor, _ = map(int, old.split("."))
    new = f"{major}.{minor + 1}.0"
    for path, text in bump.plan(new, root, today="2026-11-01").items():
        path.write_text(text)
    assert bump.current(root) == new
    assert f'version = "{new}"' in (root / "pyproject.toml").read_text()
    assert f'name = "locks"\nversion = "{new}"' in (root / "uv.lock").read_text()
    assert json.loads((root / ".claude-plugin/plugin.json").read_text())["version"] == new
    assert json.loads((root / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["version"] == new
    log = (root / "CHANGELOG.md").read_text()
    assert log.startswith(f"# Changelog\n\n## [Unreleased]\n\n## [{new}] - 2026-11-01\n\n"
                          f"### Added\n\n- a thing\n\n## [{old}]")


def test_nothing_is_written_for_a_bad_version_or_empty_notes(tmp_path):
    root = _copy(tmp_path)
    with pytest.raises(SystemExit, match="no version"):
        bump.plan("1.2", root)
    with pytest.raises(SystemExit, match="not after"):
        bump.plan(bump.current(root), root)
    empty = _copy(tmp_path / "e", unreleased="")
    with pytest.raises(SystemExit, match="empty"):
        bump.plan("99.0.0", empty)
    (root / "pyproject.toml").write_text('[project]\nname = "locks"\n')
    with pytest.raises(SystemExit, match="expected one version line"):
        bump.plan("99.0.0", root)
