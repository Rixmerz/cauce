"""What a running task shows of itself: time against its kind's usual, stage, what it wrote, a quiet worker."""
from __future__ import annotations

import subprocess
from datetime import datetime

from cauce import progress
from cauce.cli import main
from cauce.config import home

from .conftest import git
from .test_standards import finished


def _running(store, *, kind="implement", points=3, repo="r"):
    task = store.create_task("add a logout button", status="running", source="queue", repo=repo, kind=kind,
                             points=points)
    store.add_event(task["id"], "planned", kind=kind)
    store.add_event(task["id"], "attempt_started", seq=1)
    return store.get_task(task["id"])


def _at(store, task_id, kind) -> float:
    return datetime.fromisoformat(store.last_event(task_id, kind)["ts"]).timestamp()


def test_an_estimate_needs_passes_of_its_kind_and_prefers_its_size(store):
    task = _running(store)
    assert progress.estimate(store, task) is None  # no history: no guess
    for wall in (300, 400, 500):
        finished(store, ("sonnet/medium", True), points=8, wall=wall)
    found = progress.estimate(store, task)
    assert found["p50_s"] == 400 and found["by"] == "its kind"
    for wall in (60, 90, 120):
        finished(store, ("sonnet/medium", True), points=3, wall=wall)
    found = progress.estimate(store, task)
    assert found["p50_s"] == 90 and found["p90_s"] == 120 and found["by"] == "its kind and size"
    assert "≈2m here" in progress.expected_line(store, task)


def test_progress_says_the_stage_and_a_worker_gone_quiet(store):
    task = _running(store)
    began = _at(store, task["id"], "attempt_started")
    found = progress.of(store, task, now=began + 30, env={"CAUCE_HOME": str(home())})
    assert found["stage"] == "worker" and found["quiet_s"] == 30 and "no tool call" not in progress.line(found)
    quiet = progress.of(store, task, now=began + progress.IDLE_S + 60)
    assert "no tool call for 6m (a long command, or stuck)" in progress.line(quiet)
    # Once its worker is done, cauce's own check runs: no worker to be quiet.
    store.add_event(task["id"], "stage", seq=1, stage="check")
    checking = progress.of(store, task, now=began + progress.IDLE_S + 60)
    assert checking["stage"] == "cauce's check" and "quiet_s" not in checking
    assert progress.line(checking).startswith("cauce's check")
    # A new attempt is its worker's again.
    store.add_event(task["id"], "attempt_started", seq=2)
    assert progress.of(store, task)["stage"] == "worker"


def test_what_the_worker_wrote_so_far_is_read_from_its_worktree(git_repo, store):
    task = _running(store)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    path = home() / "worktrees" / f"task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", f"cauce/task-{task['id']}", str(path))
    assert progress.changes(task["id"], base) == {"files": 0, "added": 0, "removed": 0}
    (path / "new.py").write_text("a = 1\nb = 2\n")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_text("junk\n")  # a tool's artifact is no change
    assert progress.changes(task["id"], base) == {"files": 1, "added": 2, "removed": 0}
    assert progress.changes(999, base) is None  # no worktree: nothing to say


def test_the_line_says_time_against_the_usual_and_past_it():
    p = {"stage": "worker", "phase": "implementing", "elapsed_s": 240,
         "estimate": {"p50_s": 420, "p90_s": 600, "samples": 5, "by": "its kind"},
         "changes": {"files": 3, "added": 120, "removed": 8}}
    assert progress.line(p) == "implementing · 4m of ≈7m · 3 files +120 −8"
    late = {**p, "elapsed_s": 900, "changes": {"files": 0, "added": 0, "removed": 0}}
    assert progress.line(late) == ("implementing · 15m of ≈7m · nothing written yet · past 9 in 10 of its kind "
                                   "here (10m)")


def test_queueing_says_how_long_tasks_like_it_took(git_repo, store, capsys, monkeypatch):
    from cauce import repo

    monkeypatch.setenv("CAUCE_AUTOWORK", "off")
    for wall in (100, 200, 300):
        finished(store, ("sonnet/medium", True), kind="implement", repo=repo.key(git_repo), wall=wall)
    assert main(["queue", "add", "--repo", str(git_repo), "--kind", "implement", "add a logout button"]) == 0
    assert "tasks like it took ≈3m here (9 in 10 within 5m" in capsys.readouterr().out


def test_cauce_tasks_shows_a_running_task_s_progress(store, capsys):
    task = _running(store, repo=None)
    store.update_task(task["id"], source="cauce")
    main(["tasks", "--all"])
    out = capsys.readouterr().out
    assert f"#{task['id']}" in out and "\n       " in out  # its progress line under it
