from __future__ import annotations

import pytest

from cauce import isolate

from .conftest import git


def test_a_passed_task_leaves_one_branch_and_the_checkout_untouched(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 7, tmp_path / "home")
    assert ws.branch == "cauce/task-7" and ws.path.is_dir()
    (ws.path / "feature.py").write_text("x = 1\n")
    assert isolate.has_changes(ws)
    assert isolate.finish(ws, keep=True, message="cauce task #7") == "cauce/task-7"
    assert not ws.path.exists()
    assert not (git_repo / "feature.py").exists()
    assert "feature.py" in git(git_repo, "show", "--name-only", "cauce/task-7")


def test_reset_discards_a_wrong_approach(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 8, tmp_path / "home")
    (ws.path / "app.py").write_text("broken\n")
    (ws.path / "junk.txt").write_text("junk\n")
    isolate.reset(ws)
    assert (ws.path / "app.py").read_text() == "print('hi')\n"
    assert not (ws.path / "junk.txt").exists()
    assert not isolate.has_changes(ws)


def test_a_failed_task_leaves_nothing(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 9, tmp_path / "home")
    (ws.path / "x.py").write_text("x\n")
    assert isolate.finish(ws, keep=False, message="m") is None
    assert "cauce/task-9" not in git(git_repo, "branch", "--list")


def test_a_pass_that_changed_nothing_keeps_no_branch(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 10, tmp_path / "home")
    assert isolate.finish(ws, keep=True, message="m") is None


def test_outside_git_is_an_error(tmp_path):
    with pytest.raises(isolate.IsolationError):
        isolate.prepare(tmp_path, 1, tmp_path / "home")
