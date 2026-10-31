"""The release workflow's notes come from the CHANGELOG and its version from pyproject.toml."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_notes.py"
spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)


def test_a_version_s_notes_are_its_changelog_section_without_its_heading():
    log = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-31\n\n### Added\n\n- b\n\n## [0.1.0]\n\n- a\n"
    assert release_notes.notes(log, "0.2.0") == "### Added\n\n- b"
    assert release_notes.notes(log, "0.1.0") == "- a"
    with pytest.raises(SystemExit, match=r"no section for 0\.3\.0"):
        release_notes.notes(log, "0.3.0")
    with pytest.raises(SystemExit, match="empty"):
        release_notes.notes(log, "Unreleased")


def test_the_version_is_the_project_table_s():
    assert release_notes.version('[tool.x]\nversion = "9"\n[project]\nname = "c"\nversion = "1.2.3"\n') == "1.2.3"
    with pytest.raises(SystemExit):
        release_notes.version("[tool.x]\nversion = \"9\"\n")


def test_this_repository_s_version_has_notes():
    run = subprocess.run([sys.executable, str(SCRIPT), "version"], capture_output=True, text=True, check=True)
    current = run.stdout.strip()
    notes = subprocess.run([sys.executable, str(SCRIPT), "notes", current], capture_output=True, text=True, check=True)
    assert notes.stdout.strip()
