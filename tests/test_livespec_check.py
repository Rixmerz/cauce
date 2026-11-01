"""scripts/livespec_check.py: the weekly check that keeps the pinned livespec current."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("livespec_check", ROOT / "scripts" / "livespec_check.py")
check = importlib.util.module_from_spec(spec)
sys.modules["livespec_check"] = check
spec.loader.exec_module(check)

ADAPTER = '#: Checked against livespec 0.36.0.\nPINNED = "0.36.0"\n#: Verified to exist in 0.36.0.\n'
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [0.67.0] - 2026-11-01\n"


def test_the_newest_release_is_read_from_pypi_and_compared_as_numbers():
    assert check.latest(lambda url: json.dumps({"info": {"version": "0.37.0"}}).encode()) == "0.37.0"
    assert check.newer("0.36.0", "0.37.0") and check.newer("0.9.0", "0.10.0")
    assert not check.newer("0.36.0", "0.36.0") and not check.newer("0.36.0", "0.35.9")


def test_a_bump_moves_the_pin_and_its_comments_and_says_so_in_the_changelog():
    adapter, changelog = check.bumped(ADAPTER, CHANGELOG, "0.36.0", "0.37.0")
    assert check.pinned(adapter) == "0.37.0" and "0.36.0" not in adapter
    assert "livespec pinned at 0.37.0** (was 0.36.0)" in changelog.split("## [0.67.0]")[0]
    with pytest.raises(SystemExit, match=r"pins 0\.36\.0, not 0\.35\.0"):
        check.bumped(ADAPTER, CHANGELOG, "0.35.0", "0.37.0")
    with pytest.raises(SystemExit, match="no \\[Unreleased\\]"):
        check.bumped(ADAPTER, "# Changelog\n", "0.36.0", "0.37.0")
    with pytest.raises(SystemExit, match="no PINNED"):
        check.pinned("nothing here")


def test_the_command_line_says_what_it_takes(monkeypatch, capsys):
    monkeypatch.setattr(check, "latest", lambda: check.pinned())
    assert check.main(["newer"]) == 1 and "is current" in capsys.readouterr().err
    monkeypatch.setattr(check, "latest", lambda: "999.0.0")
    assert check.main(["newer"]) == 0 and capsys.readouterr().out.strip() == "999.0.0"
    assert check.main([]) == 2
    monkeypatch.setattr(check, "verify", lambda: ["it does not serve read_unit"])
    assert check.main(["verify"]) == 1 and "it does not serve read_unit" in capsys.readouterr().err


def test_the_weekly_workflow_bumps_verifies_and_opens_a_pull_request_only_after_the_suite():
    flow = (ROOT / ".github" / "workflows" / "livespec-pin.yml").read_text().split("jobs:", 1)[1]
    order = [flow.index(step) for step in ("livespec_check.py newer", "livespec_check.py bump",
                                            "livespec_check.py verify", "python -m pytest", "gh pr create")]
    head = (ROOT / ".github" / "workflows" / "livespec-pin.yml").read_text()
    assert order == sorted(order) and "schedule:" in head and "workflow_dispatch" in head
