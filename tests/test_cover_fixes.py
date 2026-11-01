"""Small fixes the coverage pass found, each with the case that showed it."""
from __future__ import annotations

import subprocess

from cauce import ci, notes


def test_an_empty_flow_mapping_in_a_ci_file_is_an_empty_mapping_never_text():
    found = ci.read("jobs:\n  test:\n    services:\n      db: {}\n    steps: []\n")
    assert found["jobs"]["test"]["services"] == {"db": {}} and found["jobs"]["test"]["steps"] == []


def test_a_checkout_with_no_commit_has_no_head(tmp_path):
    """`git rev-parse HEAD` prints `HEAD` itself when there is no commit: it was read as a commit."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert notes._head(tmp_path) == ""
    (tmp_path / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=tmp_path, check=True)
    assert len(notes._head(tmp_path)) == 40
