"""A first try that races the next rung: each in its own worktree, the first to pass its check kept."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from cauce import isolate, launch, orchestrate, repo, standards
from cauce.escalate import Failure
from cauce.launch import WorkerResult
from cauce.matrix import Cell, ladder_for
from cauce.orchestrate import Options, run

from .test_orchestrate import kind
from .test_standards import finished

START, RUNG = Cell("sonnet", "medium"), Cell("sonnet", "high")


class Racing:
    """A launcher per cell: what each writes, how long it takes, and how it ends."""

    def __init__(self, **by_cell):
        self.by_cell = by_cell
        self.specs: list[launch.LaunchSpec] = []
        self._lock = threading.Lock()

    def __call__(self, spec):
        with self._lock:
            self.specs.append(spec)
        write, delay, result = self.by_cell.get(spec.cell.label, self.by_cell.get("*"))
        time.sleep(delay)
        if write:
            (spec.target_dir / write).write_text(f"{spec.cell.label}\n")
        return result


@pytest.fixture
def racing(monkeypatch):
    """The plan races START against RUNG, whatever the history says."""
    monkeypatch.setattr(orchestrate, "race_cell", lambda *a, **kw: (RUNG, "its first try races (test)"))


def ok(summary="done"):
    return WorkerResult(True, None, summary, "1 passed", cost_usd=0.2)


def bad(summary="still broken"):
    return WorkerResult(False, Failure.CODE_BUG, summary, cost_usd=0.1)


def files(git_repo, branch):
    return subprocess.run(["git", "-C", str(git_repo), "ls-tree", "--name-only", "-r", branch],
                          capture_output=True, text=True, check=True).stdout.split()


def test_the_rung_that_passes_first_is_kept_and_the_start_is_stopped(git_repo, store, racing):
    launcher = Racing(**{START.label: ("slow.py", 0.4, ok("slow")), RUNG.label: ("fast.py", 0.0, ok("fast"))})
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"),
                 options=Options(start=None))
    assert report.status == "done" and report.final_cell == RUNG.label
    assert "fast.py" in files(git_repo, report.branch) and "slow.py" not in files(git_repo, report.branch)
    rows = store.attempts(report.task_id)
    assert [(r["cell"], r["passed"], r["move"]) for r in rows] == [
        (START.label, 0, orchestrate.RACED_OUT), (RUNG.label, 1, None)]
    assert rows[0]["summary"].startswith(f"stopped: {RUNG.label} passed its check first")
    assert report.cost_usd == pytest.approx(0.4)
    # The race's own worktree and branch are gone; the task's branch holds the kept work.
    assert not (home_worktrees() / f"task-{report.task_id}-race").exists()
    assert not isolate.kept(git_repo, f"{report.task_id}-race")
    # A stopped try teaches its cell nothing; the rung's pass is a first try of its own.
    assert standards.samples(rows) == [(RUNG.label, True)]
    started = store.events_of(report.task_id, "attempt_started")
    assert [e["data"]["cell"] for e in started] == [START.label, RUNG.label]
    assert started[1]["data"]["race_of"] == 1


def home_worktrees():
    from cauce.store import home

    return home() / "worktrees"


def test_the_start_that_passes_first_is_kept_and_the_rung_is_recorded_after_it(git_repo, store, racing):
    launcher = Racing(**{START.label: ("start.py", 0.0, ok("start")), RUNG.label: ("rung.py", 0.4, ok("rung"))})
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done" and report.final_cell == START.label
    assert "start.py" in files(git_repo, report.branch) and "rung.py" not in files(git_repo, report.branch)
    rows = store.attempts(report.task_id)
    assert [(r["cell"], r["passed"], r["move"]) for r in rows] == [
        (START.label, 1, "race"), (RUNG.label, 0, orchestrate.RACED_OUT)]
    assert standards.samples(rows) == [(START.label, True)]


def test_a_race_neither_try_won_climbs_from_the_rung(git_repo, store, racing):
    launcher = Racing(**{START.label: ("a.py", 0.0, bad()), RUNG.label: ("b.py", 0.1, bad()),
                         "*": ("c.py", 0.0, ok("third"))})
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done"
    rows = store.attempts(report.task_id)
    assert [r["cell"] for r in rows][:2] == [START.label, RUNG.label] and rows[0]["move"] == "race"
    # The rung's work is what the next attempt continues from, in the task's worktree.
    assert launcher.specs[-1].cell != START.label and len(rows) == 3
    assert standards.samples(rows)[:2] == [(START.label, False), (RUNG.label, False)]


def test_each_side_is_checked_in_its_own_worktree_and_a_red_check_loses(git_repo, store, racing):
    # The rung claims a pass first, but its own check fails there; the start's passes.
    launcher = Racing(**{START.label: ("good.py", 0.3, ok("start")), RUNG.label: ("broken.py", 0.0, ok("rung"))})
    check = f"{sys.executable} -c \"import os,sys; sys.exit(os.path.exists('broken.py'))\""
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"),
                 options=Options(verify=check))
    assert report.status == "done" and report.final_cell == START.label
    assert "good.py" in files(git_repo, report.branch)


def test_the_race_s_prompt_names_its_own_worktree(git_repo, store, racing):
    launcher = Racing(**{"*": (None, 0.0, ok())})
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    first, second = sorted(launcher.specs, key=lambda s: s.cell != START)
    assert second.launch_dir.name.endswith("-race") and str(second.launch_dir) not in first.prompt
    assert second.env["CAUCE_WORKER_ATTEMPT"].endswith("-race") and report.status == "done"


def test_no_race_without_a_worktree_or_after_bringing_files_in(git_repo, store, racing):
    launcher = Racing(**{"*": (None, 0.0, ok())})
    run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"),
        options=Options(isolate=False))
    assert len(launcher.specs) == 1


def test_a_lane_that_cannot_be_made_runs_the_try_alone(git_repo, store, racing, monkeypatch):
    def broken(*a, **kw):
        raise isolate.IsolationError("no room")

    monkeypatch.setattr(isolate, "race_lane", broken)
    launcher = Racing(**{"*": (None, 0.0, ok())})
    report = run("add the export", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "done" and len(launcher.specs) == 1


# --- when a start races ---------------------------------------------------------------------------------


def test_a_start_races_only_where_its_first_tries_rarely_pass(git_repo, store, monkeypatch):
    monkeypatch.setenv("CAUCE_RACE", "on")
    key = repo.key(git_repo)
    ladder = ladder_for("implement")
    nothing = orchestrate.race_cell(store, key, "implement", ladder, START, Options())
    assert nothing == (None, "")  # no history yet
    for _ in range(3):
        finished(store, (START.label, 0, "code_bug", "more_effort"), (RUNG.label, 1), repo=key)
    finished(store, (START.label, 1), repo=key)
    rung, why = orchestrate.race_cell(store, key, "implement", ladder, START, Options())
    assert rung == ladder[ladder.index(START) + 1] and "1 of 4 first tries" in why
    # pinned, read-only, in the checkout, a ledger step, or off: no race
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options(start=START))[0] is None
    assert orchestrate.race_cell(store, key, "explore", ladder_for("explore"), START, Options())[0] is None
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options(isolate=False))[0] is None
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options(ledger={"role": "fix"}))[0] is None
    assert orchestrate.race_cell(store, None, "implement", ladder, START, Options())[0] is None
    assert orchestrate.race_cell(store, key, "implement", ladder, ladder[-1], Options())[0] is None
    monkeypatch.setenv("CAUCE_RACE", "off")
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options())[0] is None


def test_a_start_that_passes_often_does_not_race(git_repo, store, monkeypatch):
    monkeypatch.setenv("CAUCE_RACE", "on")
    key = repo.key(git_repo)
    for _ in range(4):
        finished(store, (START.label, 1), repo=key)
    assert orchestrate.race_cell(store, key, "implement", ladder_for("implement"), START, Options())[0] is None


# --- the pieces -------------------------------------------------------------------------------------------


def test_a_stoppable_runner_runs_and_stops_its_process_group(tmp_path):
    runner = launch.Stoppable()
    done = runner([sys.executable, "-c", "import sys; print(sys.stdin.read())"], input="hi", cwd=str(tmp_path))
    assert done.returncode == 0 and done.stdout.strip() == "hi"
    slow = launch.Stoppable()
    t = threading.Thread(target=lambda: setattr(slow, "out", slow([sys.executable, "-c", "import time; "
                                                                    "time.sleep(30)"], cwd=str(tmp_path))))
    t.start()
    while slow.proc is None:
        time.sleep(0.01)
    slow.stop()
    t.join(10)
    assert not t.is_alive() and slow.out.returncode != 0
    assert slow([sys.executable, "-c", "pass"]).stderr == "stopped before it started"
    late = launch.Stoppable()
    with pytest.raises(subprocess.TimeoutExpired):
        late([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


def test_adopt_carries_a_lane_s_work_and_fails_loudly_when_it_cannot(git_repo, tmp_path):
    from cauce.store import home

    ws = isolate.prepare(git_repo, 901, home())
    lane = isolate.race_lane(ws, home())
    (lane.path / "new.py").write_text("x = 1\n")
    (lane.path / "app.py").write_text("print('bye')\n")
    (ws.path / "stray.py").write_text("left by the start\n")
    assert set(isolate.adopt(lane, ws)) == {"new.py", "app.py"}
    assert (ws.path / "new.py").read_text() == "x = 1\n" and not (ws.path / "stray.py").exists()
    isolate.drop(lane)
    assert not lane.path.exists()
    again = isolate.race_lane(ws, home())  # made again over an old one
    (again.path / "app.py").write_text("print('again')\n")
    (ws.path / ".git").unlink()  # the task's worktree is broken: the work cannot land
    with pytest.raises(isolate.IsolationError):
        isolate.adopt(again, ws)
