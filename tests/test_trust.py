from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest

from locks import isolate, orchestrate, standards, trust
from locks.launch import WorkerResult
from locks.matrix import PROBATION, Cell, reach
from locks.orchestrate import Options, run
from locks.store import Store

from .test_checking import BUG, OTHER, check, passing
from .test_orchestrate import Script, kind
from .test_standards import NOW, finished, many

HAIKU = Cell("haiku", "high")
FIRST = Cell("sonnet", "medium")


def inspected(store: Store, outcome: str, *, repo="r", kind_="implement", cell="haiku/high", blockers=(),
              round_=0, late=False, worker=True) -> int:
    """A task whose first independent check came `clean` or `blocked`, or that the sampling let through."""
    task = store.create_task("t", status="done", source="locks", repo=repo, kind=kind_, points=3)
    store.add_attempt(task["id"], cell=cell, max_turns=30, passed=1)
    named = {"worker": cell} if worker else {}
    if outcome == "sampled":
        store.add_event(task["id"], "verifier_sampled", seq=1, every=2, **named)
    else:
        store.add_event(task["id"], "verified", seq=1, round=round_, late=late, passed=outcome == "clean",
                        blocking=len(blockers) if outcome == "blocked" else 0, blockers=list(blockers), **named)
    return task["id"]


# --- skip-lot sampling -----------------------------------------------------------------------------

def _level(*outcomes: str) -> trust.Level:
    return trust.level([trust.Inspection(i, "r", "implement", "haiku/high", o, "") for i, o in enumerate(outcomes)])


def test_ten_clean_checks_in_a_row_halve_the_checks_and_ten_more_halve_them_again_never_further():
    assert _level(*["clean"] * (trust.QUALIFY - 1)).every == 1
    assert _level(*["clean"] * trust.QUALIFY).every == 2
    assert _level(*["clean"] * (2 * trust.QUALIFY)).every == 4
    assert _level(*["clean"] * (5 * trust.QUALIFY)).every == trust.EVERY[-1] == 4


def test_one_blocker_returns_a_cell_to_every_pass_and_it_qualifies_again_from_zero():
    record = _level(*["clean"] * (2 * trust.QUALIFY), "blocked")
    assert (record.every, record.run, record.blocked) == (1, 0, 1)
    assert _level(*["clean"] * trust.QUALIFY, "blocked", *["clean"] * (trust.QUALIFY - 1)).every == 1


def test_a_pass_let_through_neither_qualifies_nor_resets():
    record = _level(*["clean"] * trust.QUALIFY, *["sampled"] * 30)
    assert (record.every, record.run, record.sampled, record.checks) == (2, 0, 30, trust.QUALIFY)


def test_which_passes_are_checked_is_a_hash_of_the_repository_and_the_task():
    assert all(trust.due("r", n, 1) for n in range(20))
    drawn = [trust.due("r", n, 4) for n in range(400)]
    assert drawn == [trust.due("r", n, 4) for n in range(400)]  # a resume decides as its first run did
    assert 60 < sum(drawn) < 140  # about one in four


def test_inspections_read_each_task_s_first_decided_check_with_the_cell_it_read(store: Store):
    clean = inspected(store, "clean")
    inspected(store, "blocked", blockers=[BUG])
    later = inspected(store, "clean")
    store.add_event(later, "verified", seq=1, round=1, passed=False, blocking=1, blockers=[BUG])  # a re-check
    store.add_event(clean, "verified", seq=2, round=0, passed=False, blocking=1, blockers=[BUG])  # not its first
    undecided = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    store.add_event(undecided["id"], "verified", seq=1, round=0, passed=False, blocking=0, worker="haiku/high")
    old = inspected(store, "clean", worker=False)  # before events named the cell: the attempt's is read
    inspected(store, "sampled")
    found = trust.inspections(store, "r")
    assert [i.outcome for i in found] == ["clean", "blocked", "clean", "clean", "sampled"]
    assert found[1].blockers == (BUG,) and found[3].task == old and {i.cell for i in found} == {"haiku/high"}
    assert trust.inspections(store, "elsewhere") == []


def test_only_a_qualified_cell_s_passes_go_unchecked_and_never_a_high_risk(store: Store, monkeypatch):
    for _ in range(trust.QUALIFY):
        inspected(store, "clean")
    assert trust.sampling(store, "r", "implement", HAIKU, 1, tier="medium").check  # trust is off in tests
    monkeypatch.setenv("LOCKS_TRUST", "on")
    drawn = [trust.sampling(store, "r", "implement", HAIKU, n, tier="medium") for n in range(40)]
    skipped = [s for s in drawn if not s.check]
    assert skipped and len(skipped) < 40 and all(s.every == 2 for s in drawn)
    assert skipped[0].why.startswith("its independent check was not sampled: implement work at haiku/high here is "
                                     "checked one pass in 2")
    assert all(trust.sampling(store, "r", "implement", HAIKU, n, tier="high").check for n in range(40))
    # another cell, kind or repository has a record of its own
    assert trust.sampling(store, "r", "implement", Cell("sonnet", "low"), 3, tier="medium").every == 1
    assert trust.sampling(store, "r", "docs", HAIKU, 3, tier="medium").every == 1
    assert trust.sampling(store, None, "implement", HAIKU, 3, tier="medium").check


# --- the record ------------------------------------------------------------------------------------

def test_the_report_counts_repeated_blockers_new_findings_on_re_checks_and_cost_per_point(store: Store):
    first = inspected(store, "blocked", blockers=[BUG])
    reworded = {**BUG, "where": "app.py:7", "what": "it drops the last item again"}
    inspected(store, "blocked", blockers=[reworded, OTHER])
    inspected(store, "clean")
    # a correction's re-check that found something the round before had not, and one that did not
    store.add_event(first, "verified", seq=2, round=1, passed=False, blocking=2, blockers=[BUG, OTHER],
                    worker="haiku/high")
    store.add_event(first, "verified", seq=3, round=2, passed=False, blocking=1, blockers=[OTHER],
                    worker="haiku/high")
    many(store, 3, ("haiku/high", 1), cost=0.3)
    finished(store, ("haiku/high", 0, "code_bug", "next_model"), ("sonnet/medium", 1), cost=0.9)
    row, = [r for r in trust.report(store, "r") if r["cell"] == "haiku/high"]
    assert (row["checks"], row["clean"], row["blocked"], row["every"]) == (3, 1, 2, 1)
    assert (row["blockers"], row["repeated"]) == (3, 1) and row["repeat_rate"] == round(1 / 3, 3)
    assert (row["rechecks"], row["rechecks_new"]) == (2, 1)
    assert row["started"] == 4 and row["pass_first"] == 0.75
    assert row["cost_per_point"] == round((3 * 0.3 + 0.9) / (4 * 3), 4)
    line, = [x for x in trust.text(trust.report(store, "r")) if "haiku/high" in x]
    assert "repeated blockers 1/3" in line and "re-checks finding new 1/2" in line and "every pass" in line


# --- the probation rung ----------------------------------------------------------------------------

def test_the_probation_cell_lies_below_the_measured_ladders_only():
    assert reach("implement")[0] == PROBATION["implement"] == HAIKU
    assert reach("refactor")[0] == reach("test")[0] == Cell("haiku", "medium")
    assert reach("implement")[1:] == standards.ladder_for("implement")
    assert reach("feature") == standards.ladder_for("feature")


def _arm(store, start=FIRST, **kw):
    kw = {"tier": "medium", "eligible": True, **kw}
    return trust.probation_arm(store, "r", "implement", 3, start, **kw)


def _plan(store, arm):
    """A task planned in an arm of the trial, as `orchestrate.run` records it."""
    task = store.create_task("t", status="running", source="locks", repo="r", kind="implement", points=3)
    store.add_event(task["id"], "planned", probation={"arm": arm, "cell": "haiku/high"})


def test_a_trial_runs_only_where_the_first_rung_passes_and_its_arms_alternate(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_PROBATION", "off")
    many(store, 6, ("sonnet/medium", 1))
    assert _arm(store) == (None, "")  # probation is off
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    arm, why = _arm(store)
    assert arm == "probation" and why.startswith("on trial at haiku/high, below the implement ladder: 6 of 6 first "
                                                 "tries at sonnet/medium passed here")
    _plan(store, "probation")
    assert _arm(store)[0] == "ladder"
    _plan(store, "ladder")
    assert _arm(store)[0] == "probation"
    # never a raised or pinned start, a high risk, or with the verifier off
    assert _arm(store, start=Cell("sonnet", "high")) == (None, "")
    assert _arm(store, eligible=False) == (None, "")
    assert _arm(store, tier="high") == (None, "")
    monkeypatch.setenv("LOCKS_VERIFIER", "off")
    assert _arm(store) == (None, "")


def test_no_trial_where_the_first_rung_does_not_pass_often_enough(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    many(store, 4, ("sonnet/medium", 1))
    assert _arm(store) == (None, "")  # too few
    many(store, 3, ("sonnet/medium", 0, "code_bug", "more_effort"), ("sonnet/high", 1))
    assert _arm(store) == (None, "")  # 4 of 7 is under the threshold


def test_a_trial_ends_once_its_cell_was_tried_enough_or_lost_early(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    many(store, 6, ("sonnet/medium", 1))
    for _ in range(trust.PROBATION_TRIAL):
        _plan(store, "probation")
        _plan(store, "ladder")
    assert _arm(store) == (None, "")
    store._conn.execute("DELETE FROM events WHERE kind = 'planned' AND json_extract(data, '$.probation') IS NOT NULL")
    many(store, trust.PROBATION_EARLY, ("haiku/high", 0, "code_bug", "next_model"), ("sonnet/medium", 1),
         at=NOW + timedelta(days=3650))
    assert _arm(store) == (None, "")


def test_the_calibration_moves_onto_the_probation_cell_only_by_its_own_rule(store: Store, monkeypatch):
    many(store, 6, ("sonnet/medium", 1))
    many(store, 5, ("haiku/high", 1))
    monkeypatch.setenv("LOCKS_PROBATION", "off")
    assert standards.calibrate(store, "r", now=NOW) == []  # off: the ladder's first rung is the floor
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    change, = standards.calibrate(store, "r", now=NOW)
    assert (change["field"], change["old_value"], change["new_value"]) == ("start_cell", None, "haiku/high")
    assert change["rule"].endswith("(its probation cell, below the ladder)")
    used = standards.for_plan(store, "r", "implement", 3, pinned=False, explicit_turns=False)
    assert used.cell == HAIKU
    monkeypatch.setenv("LOCKS_PROBATION", "off")
    gone = standards.for_plan(store, "r", "implement", 3, pinned=False, explicit_turns=False)
    assert gone.cell is None and "no longer holds" in gone.reasons[0]


def test_without_enough_cheap_tries_locks_s_own_start_stays_put(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    many(store, 6, ("sonnet/medium", 1))
    many(store, 4, ("haiku/high", 1))
    assert standards.calibrate(store, "r", now=NOW) == []


def _onto_haiku(store, monkeypatch) -> dict:
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    many(store, 6, ("sonnet/medium", 1), cost=1.0, at=NOW - timedelta(days=3))
    many(store, 5, ("haiku/high", 1), cost=0.2, at=NOW - timedelta(days=3))
    change, = standards.calibrate(store, "r", now=NOW - timedelta(days=2))
    return change


def test_a_probation_start_that_costs_more_per_point_is_reverted(store: Store, monkeypatch):
    change = _onto_haiku(store, monkeypatch)
    many(store, standards.JUDGE_AFTER, ("haiku/high", 1), cost=2.0, used=[change["id"]], at=NOW)
    verdict, = standards.judge(store, "r", now=NOW)
    assert verdict["verdict"] == "reverted"
    revert = store.calibrations(repo="r")[-1]
    assert revert["reverts"] == change["id"] and "costs more on the probation cell" in revert["rule"]
    assert "a passed point cost" in store.verdicts()[change["id"]]["reason"]
    assert store.standard("r", "implement", 3)["start_cell"] is None


def test_a_probation_start_whose_points_take_far_longer_is_reverted(store: Store, monkeypatch):
    change = _onto_haiku(store, monkeypatch)
    many(store, standards.JUDGE_AFTER, ("haiku/high", 1), cost=0.2, wall=600.0 * 5, used=[change["id"]], at=NOW)
    assert standards.judge(store, "r", now=NOW)[0]["verdict"] == "reverted"
    revert = store.calibrations(repo="r")[-1]
    assert "times as long on the probation cell" in revert["rule"] and "17 min after" in revert["rule"]


def test_a_probation_start_that_costs_less_and_passes_as_often_is_kept(store: Store, monkeypatch):
    change = _onto_haiku(store, monkeypatch)
    many(store, standards.JUDGE_AFTER, ("haiku/high", 1), cost=0.2, used=[change["id"]], at=NOW)
    assert standards.judge(store, "r", now=NOW)[0]["verdict"] == "kept"
    assert store.standard("r", "implement", 3)["start_cell"] == "haiku/high"


def test_a_plan_on_trial_starts_on_the_probation_cell_and_climbs_into_the_ladder(git_repo, store, monkeypatch):
    from locks import repo

    key = repo.key(git_repo)
    many(store, 6, ("sonnet/medium", 1), repo=key, points=1)
    monkeypatch.setenv("LOCKS_PROBATION", "on")
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    script = Script(WorkerResult(False, orchestrate.Failure.APPROACH, "wrong", cost_usd=0.01), passing(),
                    write="notes.txt")
    report = run("rename the helper in app.py", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[])
    assert report.cells == ["haiku/high", "sonnet/medium"] and report.status == "done"
    planned = store.last_event(report.task_id, "planned")["data"]
    assert planned["probation"] == {"arm": "probation", "cell": "haiku/high"}
    assert planned["ladder"][0] == "haiku/high" and any(r.startswith("on trial at haiku/high") for r in
                                                        planned["reasons"])
    second = run("rename the other helper in app.py", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("implement"), adapters=[])
    assert second.plan.start == Cell("sonnet", "medium") and second.plan.probation["arm"] == "ladder"


# --- the orchestrator: a sampled pass, and a correction's re-check of what changed --------------------

def test_a_qualified_cell_s_pass_the_sampling_does_not_draw_goes_unchecked_and_says_so(git_repo, store, monkeypatch):
    from locks import repo

    key = repo.key(git_repo)
    for _ in range(trust.QUALIFY):
        inspected(store, "clean", repo=key)
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    monkeypatch.setenv("LOCKS_TRUST", "on")
    monkeypatch.setattr(trust, "due", lambda repo_, task_id, every: False)
    script = Script(passing(), write="notes.txt")  # no code: the risk stays medium
    report = run("rename the helper in app.py", git_repo, store, Options(start=HAIKU), registry={},
                 launcher=script, classifier=kind("implement"), adapters=[])
    assert report.status == "done" and len(script.specs) == 1  # no verifier was launched
    sampled, = store.events_of(report.task_id, "verifier_sampled")
    assert sampled["data"]["worker"] == "haiku/high" and sampled["data"]["every"] == 2
    card = store.last_event(report.task_id, "finished")["data"]["verdict"]["independent"]
    assert card["ran"] is False and card["sampled"] is True and "not sampled" in card["why"]
    # the next one drawn is checked, and its check names the cell it read
    monkeypatch.setattr(trust, "due", lambda repo_, task_id, every: True)
    script = Script(passing(), check("pass"), write="notes.txt")
    report = run("rename the other helper in app.py", git_repo, store, Options(start=HAIKU), registry={},
                 launcher=script, classifier=kind("implement"), adapters=[])
    verified, = store.events_of(report.task_id, "verified")
    assert len(script.specs) == 2 and verified["data"]["worker"] == "haiku/high"
    assert verified["data"]["sampled"].startswith("sampled for its independent check")


def test_a_correction_s_re_check_reads_what_the_correction_changed_since(git_repo, store, monkeypatch):
    monkeypatch.setenv("LOCKS_VERIFIER", "on")
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    first, recheck = script.specs[1], script.specs[3]
    events = store.events_of(report.task_id, "verified")
    assert events[0]["data"]["read_at"] and "delta" not in events[0]["data"]
    assert "This is a correction's re-check" not in first.prompt
    sha = events[0]["data"]["read_at"]
    assert f"snapshot commit {sha[:12]}; since then the correction changed 1 file(s): app.py" in recheck.prompt
    assert events[1]["data"]["delta"] == {"since": sha, "files": ["app.py"], "n": 1}


def test_a_re_check_after_a_new_model_reads_the_whole_change(store: Store, tmp_path):
    task = store.create_task("t", status="running", source="locks", repo="r", kind="implement")
    store.add_event(task["id"], "verified", seq=1, round=0, read_at="abc", worker="sonnet/xhigh")
    assert orchestrate._read_before(store, task["id"], Cell("opus", "high"), 1, tmp_path) is None
    assert orchestrate._read_before(store, task["id"], Cell("sonnet", "xhigh"), 0, tmp_path) is None
    assert orchestrate._read_before(store, task["id"], Cell("sonnet", "xhigh"), 1, None) is None
    assert "nothing changed since" in orchestrate.recheck_lines("abcdef0123456789", [])
    many_files = orchestrate.recheck_lines("abcdef0123456789", [f"f{n}.py" for n in range(25)])
    assert "25 file(s)" in many_files and "and 5 more" in many_files


# --- the snapshot ----------------------------------------------------------------------------------

def test_a_snapshot_keeps_untracked_files_and_leaves_the_index_and_branch_alone(git_repo):
    (git_repo / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "t"], cwd=git_repo, check=True)
    (git_repo / "tracked.txt").write_text("two\n")
    (git_repo / "new.txt").write_text("new\n")
    head = isolate.head(git_repo, "HEAD")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True).stdout
    sha = isolate.snapshot(git_repo)
    assert sha and isolate.head(git_repo, "HEAD") == head
    assert subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True,
                          text=True).stdout == status
    shown = subprocess.run(["git", "show", f"{sha}:new.txt"], cwd=git_repo, capture_output=True, text=True).stdout
    assert shown == "new\n"
    assert isolate.changed_since(git_repo, sha) == []
    (git_repo / "tracked.txt").write_text("three\n")
    (git_repo / "later.txt").write_text("later\n")
    assert isolate.changed_since(git_repo, sha) == ["later.txt", "tracked.txt"]


def test_a_snapshot_of_no_checkout_is_none(tmp_path):
    assert isolate.snapshot(tmp_path) is None
    assert isolate.changed_since(tmp_path, "abc") is None


def test_the_cli_prints_the_record(store: Store, capsys, monkeypatch, tmp_path):
    from locks import cli

    inspected(store, "clean", repo=None)
    assert cli.main(["trust", "--all", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["cell"] == "haiku/high" and rows[0]["clean"] == 1
    assert cli.main(["trust", "--all"]) == 0
    assert "haiku/high" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    assert cli.main(["trust"]) == 0
    assert "No independent check yet" in capsys.readouterr().out


@pytest.mark.parametrize("tier", ["high", "passive"])
def test_only_a_medium_change_a_cheap_cell_made_is_ever_sampled(store: Store, tier, monkeypatch):
    monkeypatch.setenv("LOCKS_TRUST", "on")
    for _ in range(trust.QUALIFY * 2):
        inspected(store, "clean")
    task = {"id": 1, "repo": "r"}
    plan = orchestrate.Plan("implement", None, (), HAIKU, [], (), risk=orchestrate.risk_mod.Risk(tier))  # type: ignore[arg-type]
    assert orchestrate._sampling(store, task, plan, HAIKU, Options(), 0, {}).check
    medium = orchestrate.Plan("implement", None, (), HAIKU, [], (), risk=orchestrate.risk_mod.Risk("medium"))  # type: ignore[arg-type]
    assert orchestrate._sampling(store, task, medium, Cell("opus", "high"), Options(), 0, {}).check  # not cheap
    assert orchestrate._sampling(store, task, medium, HAIKU, Options(), 1, {}).check  # a re-check
    assert orchestrate._sampling(store, task, medium, HAIKU, Options(), 0, {"k": BUG}).check  # carried blockers
    assert orchestrate._sampling(store, task, medium, HAIKU, Options(), 0, {}).every == 4


def test_an_ending_says_a_pass_its_sampling_let_through():
    from locks import hooks

    verdict = {"check": {"command": "pytest", "passed": True}, "criteria": [],
               "independent": {"ran": False, "sampled": True, "every": 4, "why": "its independent check was not "
                               "sampled: implement work at haiku/high here is checked one pass in 4"}}
    assert "independent check not drawn (1 pass in 4 here)" in hooks.verdict_summary(verdict)
    assert "  independent check: its independent check was not sampled: implement work at haiku/high here is " \
           "checked one pass in 4" in hooks.verdict_lines(verdict)
