"""A first try that races the next rung: each in its own worktree, the first to pass its check kept."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from locks import analysis, isolate, launch, orchestrate, repo, standards
from locks import racing as racing_mod
from locks.escalate import Failure
from locks.launch import WorkerResult
from locks.matrix import Cell, ladder_for
from locks.orchestrate import Options, run

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


@pytest.mark.usefixtures("no_verifier")
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
    from locks.store import home

    return home() / "worktrees"


@pytest.mark.usefixtures("no_verifier")
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
    assert second.env["LOCKS_WORKER_ATTEMPT"].endswith("-race") and report.status == "done"


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
    monkeypatch.setenv("LOCKS_RACE", "on")
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
    monkeypatch.setenv("LOCKS_RACE", "off")
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options())[0] is None


def test_a_start_whose_failed_first_tries_ran_out_of_turns_does_not_race(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_RACE", "on")
    key = repo.key(git_repo)
    ladder = ladder_for("implement")
    for _ in range(2):
        finished(store, (START.label, 0, "turns_exhausted", "more_turns"), (START.label, 0, "turns_exhausted"),
                 repo=key)
    finished(store, (START.label, 0, "code_bug", "more_effort"), (RUNG.label, 1), repo=key)
    finished(store, (START.label, 1), repo=key)
    rung, why = orchestrate.race_cell(store, key, "implement", ladder, START, Options())
    assert rung is None and "2 of the 3 that failed ran out of turns, which another cell does not fix" in why
    # when most of them failed on the work itself, it races
    for _ in range(2):
        finished(store, (START.label, 0, "code_bug", "more_effort"), (RUNG.label, 1), repo=key)
    assert orchestrate.race_cell(store, key, "implement", ladder, START, Options())[0] == RUNG


def test_a_start_that_passes_often_does_not_race(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_RACE", "on")
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
    from locks.store import home

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


# --- whether racing paid: wide or deep, by its own record --------------------------------------------------

THIRD = Cell("opus", "high")


def rung_won(store, key, wall=300.0):
    return finished(store, (START.label, 0, "code_bug", orchestrate.RACED_OUT), (RUNG.label, 1), repo=key, wall=wall)


def start_won(store, key, wall=300.0):
    return finished(store, (START.label, 1, None, "race"), (RUNG.label, 0, "code_bug", orchestrate.RACED_OUT),
                    repo=key, wall=wall)


def neither_won(store, key, wall=900.0):
    return finished(store, (START.label, 0, "code_bug", "race"), (RUNG.label, 0, "code_bug", "next_model"),
                    (THIRD.label, 1), repo=key, wall=wall)


def climbed(store, key, wall=900.0):
    return finished(store, (START.label, 0, "code_bug", "more_effort"), (RUNG.label, 1), repo=key, wall=wall)


def test_a_race_is_read_from_its_attempts():
    a = lambda move=None, passed=0: {"move": move, "passed": passed}  # noqa: E731
    assert racing_mod.outcome([a(racing_mod.RACED_OUT), a(None, 1)]) == racing_mod.RUNG
    assert racing_mod.outcome([a("race", 1), a(racing_mod.RACED_OUT)]) == racing_mod.START
    assert racing_mod.outcome([a("race"), a("more_effort"), a(None, 1)]) == racing_mod.NEITHER
    assert racing_mod.outcome([a("more_effort"), a(None, 1)]) is None
    assert racing_mod.outcome([a(None, 1)]) is None
    assert racing_mod.RACED_OUT == orchestrate.RACED_OUT == "raced_out"


def test_racing_is_judged_only_after_enough_races(store):
    for _ in range(racing_mod.JUDGE_AFTER - 1):
        neither_won(store, "r")
    record = racing_mod.of(standards.ended(store, repo="r"), "implement")
    assert record.pays and record.races == racing_mod.JUDGE_AFTER - 1 and record.neither == racing_mod.JUDGE_AFTER - 1
    assert f"judged after {racing_mod.JUDGE_AFTER}" in record.why


def test_a_rung_that_rarely_wins_stops_the_race_and_the_climb_goes_deep(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_RACE", "on")
    key = repo.key(git_repo)
    ladder = ladder_for("implement")
    for _ in range(4):
        neither_won(store, key)
    start_won(store, key)
    record = racing_mod.of(standards.ended(store, repo=key), "implement")
    assert (record.races, record.rung_wins, record.start_wins, record.neither) == (5, 0, 1, 4) and not record.pays
    rung, why = orchestrate.race_cell(store, key, "implement", ladder, START, Options())
    assert rung is None and "does not race" in why and "the rung won 0 of 5 races here" in why
    assert "climbs deep instead" in why
    # The plan says so in its line, beside no rung.
    the_plan = orchestrate.plan("add the export", git_repo, store, Options(use_model_classifier=False), {},
                                classifier=kind("implement"), adapters=())
    assert the_plan.race is None and the_plan.start == START and why in the_plan.reasons


def test_racing_that_pays_keeps_racing_and_says_why(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_RACE", "on")
    key = repo.key(git_repo)
    for _ in range(4):
        climbed(store, key, wall=900.0)
    for _ in range(3):
        rung_won(store, key, wall=300.0)
    for _ in range(2):
        neither_won(store, key, wall=600.0)
    record = racing_mod.of(standards.ended(store, repo=key), "implement")
    assert record.pays and record.rung_wins == 3 and record.raced_p50_s == 300.0 and record.climb_p50_s == 900.0
    rung, why = orchestrate.race_cell(store, key, "implement", ladder_for("implement"), START, Options())
    assert rung == RUNG and "racing pays: the rung won 3 of 5 races here" in why
    assert "300s at the median against 900s for climbs" in why


def test_races_that_save_no_time_stop_racing(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_RACE", "on")
    key = repo.key(git_repo)
    for _ in range(4):
        climbed(store, key, wall=500.0)
    for _ in range(5):
        rung_won(store, key, wall=1000.0)
    record = racing_mod.of(standards.ended(store, repo=key), "implement")
    assert not record.pays and "racing saved no time" in record.why
    rung, why = orchestrate.race_cell(store, key, "implement", ladder_for("implement"), START, Options())
    assert rung is None and "racing saved no time" in why
    # With too few climbs to compare, time says nothing and the wins decide.
    assert racing_mod.judge(standards.ended(store, repo=key)[-5:]).pays


def test_stats_show_each_repository_and_kind_s_races(store):
    for _ in range(3):
        rung_won(store, "/work/shop")
    start_won(store, "/work/shop")
    neither_won(store, "/work/shop")
    finished(store, (START.label, 1), repo="/work/shop", kind="bugfix")
    report = analysis.compute(store, days=0)
    assert report["races"] == [{"repo": "/work/shop", "kind": "implement", "races": 5, "rung_wins": 3,
                                "start_wins": 1, "neither": 1, "raced_p50_s": 300.0, "climb_p50_s": None,
                                "pays": True, "why": report["races"][0]["why"]}]
    shown = analysis.text(report)
    assert "first tries that raced the next rung" in shown
    assert "shop" in shown and "races   5  rung won   3  start won   1  both failed   1  racing: racing pays" in shown
