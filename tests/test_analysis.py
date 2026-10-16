from __future__ import annotations

import csv
import io
import json

from cauce import analysis, cli
from cauce.escalate import Failure
from cauce.launch import WorkerResult
from cauce.orchestrate import Options, run
from cauce.store import home
from cauce.ui import api

from .test_orchestrate import Script, bad, kind, ok
from .test_survey import user, write


def refused():
    return WorkerResult(False, Failure.PERMISSION, "the migration was refused", denied=("Bash(alembic upgrade)",),
                        allow=("Bash(alembic:*)",), cost_usd=0.05)


def seed(git_repo, tmp_path, store):
    """Four runs: a first-try pass, a climb, a refusal, and a queued pass a session sent."""
    run("fix the cart total", git_repo, store, registry={}, launcher=Script(ok(cost=0.2)), classifier=kind("implement"))
    run("fix the invoice rounding", git_repo, store, registry={},
        launcher=Script(bad(Failure.CODE_BUG, "missed it", cost=0.1), ok(cost=0.3)), classifier=kind("implement"))
    run("migrate the totals table", git_repo, store, registry={}, launcher=Script(refused()),
        classifier=kind("implement"))
    write(tmp_path / "claude-config", "-repo", "sess", [user("the cart must keep its coupon field", git_repo)])
    queued = store.enqueue("add the coupon field to the cart", repo="r", cwd=str(git_repo), session_id="sess")
    run(queued["body"], git_repo, store, Options(), registry={}, launcher=Script(ok(cost=0.4)),
        classifier=kind("feature"), task_id=queued["id"])


def test_the_analysis_counts_passes_climbs_phases_and_the_session_s_thread(git_repo, tmp_path, store):
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=30)
    s = report["summary"]
    assert (s["runs"], s["tasks"], s["passed"]) == (4, 4, 3)
    assert s["pass_rate"] == 0.75 and s["first_try_rate"] == 0.5 and s["climbed_rate"] == 0.25
    assert s["cost_usd"] == 1.05 and s["cost_per_pass_usd"] == 0.35 and s["refused_rate"] == 0.25
    assert s["wall_p50_s"] is not None and s["queued_p50_s"] is not None
    kinds = {row["kind"]: row for row in report["by_kind"]}
    assert kinds["implement"]["runs"] == 3 and kinds["feature"]["pass_rate"] == 1.0
    assert {row["source"] for row in report["by_source"]} == {"cauce"}
    # Every run here was timed, phase by phase.
    assert report["phases"]["runs"] == 4 and set(report["phases"]["seconds"]) == set(analysis.PHASES)
    assert abs(sum(report["phases"]["share"].values()) - 1) < 0.01 or sum(report["phases"]["seconds"].values()) == 0
    cells = {row["cell"]: row for row in report["cells"]}
    assert cells["sonnet/medium"]["attempts"] == 3 and cells["sonnet/medium"]["failures"] == {"code_bug": 1,
                                                                                              "permission": 1}
    assert report["stops"] == {"permission": 1} and report["moves"] == {"more_effort": 1, "blocked": 1}
    # Only the queued run was sent by a session, and its brief had the thread.
    assert report["context"] == [{"group": "with the session's thread", **analysis._summary(
        [r for r in analysis.runs(*store.runs_since("")) if r["has_session"]])}]
    assert len(report["slowest"]) == 4 and report["costliest"][0]["cost_usd"] == 0.4
    assert report["daily"][0]["runs"] == 4
    assert analysis.compute(store, days=30, repo="nowhere")["summary"]["runs"] == 0


def test_a_run_recorded_before_its_phases_were_timed_counts_by_its_timestamps(store):
    task = store.create_task("old work", status="done", source="cauce", kind="test")
    for stamp, kind_, data in (
            ("2026-10-01T10:00:00+00:00", "planned", {"kind": "test", "start": "sonnet/medium"}),
            ("2026-10-01T10:04:00+00:00", "attempt_finished", {"cell": "sonnet/medium", "passed": True,
                                                               "cost_usd": 0.2, "turns": 9, "duration_s": 200}),
            ("2026-10-01T10:05:00+00:00", "finished", {"status": "done", "final_cell": "sonnet/medium",
                                                       "cost_usd": 0.21, "stop": None})):
        store._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, ?, ?)",
                            (task["id"], stamp, kind_, json.dumps(data)))
    found = analysis.runs(*store.runs_since(""))
    assert found[0]["wall_s"] == 300 and found[0]["worker_s"] == 200 and found[0]["check_s"] is None
    assert found[0]["first_try"] is True and found[0]["cost_usd"] == 0.21
    # Its phases were never measured: the breakdown leaves it out instead of guessing.
    assert analysis.phases(found)["runs"] == 0
    # A run still going has no end yet, and an event before any plan belongs to no run.
    open_task = store.create_task("going", status="running", source="queue")
    store.add_event(open_task["id"], "attempt_started", seq=1)
    store.add_event(open_task["id"], "planned", kind="ui")
    assert [r["task"] for r in analysis.runs(*store.runs_since(""))] == [task["id"]]


def test_a_session_s_turns_and_delegations_are_timed_and_costed(store):
    turn = store.create_task("a prompt", status="done", source="hook", session_id="s")
    store.create_task("cut short", status="interrupted", source="hook", session_id="s")
    store.create_task("a subagent", status="done", source="delegation", session_id="s")
    store.add_usage(message_id="m1", session_id="s", task_id=turn["id"], repo=None, model="claude-sonnet-5-5",
                    input_tokens=100_000, output_tokens=10_000, cache_read=0, cache_write=0, sidechain=0, ts="x")
    out = analysis.turns(store.turns_since(""))
    assert out["turns"]["count"] == 2 and out["turns"]["interrupted"] == 1 and out["turns"]["usd"] > 0
    assert out["turns"]["seconds_p50"] is not None and out["delegations"]["count"] == 1
    assert sum(out["by_day"].values()) == 2


def test_percentiles_and_the_outputs(git_repo, tmp_path, store):
    assert analysis.pct([], 0.5) is None and analysis.pct([None, 3], 0.9) == 3
    assert analysis.pct([1, 2, 3, 4], 0.5) == 2 and analysis.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert analysis._seconds(None) == "—" and analysis._seconds(90) == "90s" and analysis._seconds(600) == "10.0m"
    assert analysis._seconds(9000) == "2.5h" and analysis._between("x", None) is None
    assert analysis._between("2026-10-01 10:00:00", "2026-10-01T10:01:00+00:00") == 60  # no zone: UTC
    empty = analysis.compute(store, days=7)
    assert "No run of cauce's yet" in analysis.text(empty)
    seed(git_repo, tmp_path, store)
    report = analysis.compute(store, days=0, keep_runs=None)
    shown = analysis.text(report)
    assert "runs 4 (4 tasks)" in shown and "by kind:" in shown and "where the time went" in shown
    assert "stopped by: permission 1" in shown and "session's thread" in shown
    rows = list(csv.DictReader(io.StringIO(analysis.runs_csv(report["runs"]))))
    assert len(rows) == 4 and rows[0]["kind"] == "implement" and rows[0]["status"] == "done"
    assert json.loads(analysis.as_json(report))["summary"]["runs"] == 4
    assert api.analysis_view(store, days=0)["summary"]["runs"] == 4


def test_the_stats_command(git_repo, tmp_path, store, capsys):
    seed(git_repo, tmp_path, store)
    store._conn.commit()
    assert cli.main(["stats"]) == 0
    assert "cauce stats — every repository, last 30 days" in capsys.readouterr().out
    assert cli.main(["stats", "--json", "--days", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["passed"] == 3
    assert cli.main(["stats", "--runs"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert cli.main(["stats", "--runs", "--csv", "--repo", str(git_repo)]) == 0
    assert capsys.readouterr().out.startswith("task,repo,kind")
    assert home().is_dir()


def test_a_check_s_cost_and_the_cache_are_kept_apart(store):
    task = store.create_task("checked work", status="done", source="cauce", kind="implement")
    for kind_, data in (
            ("planned", {"kind": "implement", "start": "sonnet/medium", "plan_s": 1.0}),
            ("attempt_finished", {"cell": "sonnet/medium", "passed": True, "cost_usd": 0.5, "worker_cost_usd": 0.3,
                                  "verifier_cost_usd": 0.2, "input_tokens": 10_000, "cache_read_tokens": 8_000,
                                  "wall_s": 100}),
            ("finished", {"status": "done", "cost_usd": 0.5, "wall_s": 120})):
        store.add_event(task["id"], kind_, **data)
    old = store.create_task("older work", status="done", source="cauce", kind="implement")
    for kind_, data in (("planned", {"kind": "implement"}),
                        ("verified", {"passed": True, "cost_usd": 0.1}),
                        ("attempt_finished", {"cell": "sonnet/medium", "passed": True, "cost_usd": 0.4,
                                              "input_tokens": 50_000}),
                        ("finished", {"status": "done", "cost_usd": 0.4})):
        store.add_event(old["id"], kind_, **data)
    report = analysis.compute(store, days=0)
    cell = report["cells"][0]
    # The cell's own cost leaves its check out (an older attempt's check is read from its `verified`
    # event); the cache share counts only attempts that recorded it.
    assert cell["cost_usd"] == 0.6 and cell["verifier_cost_usd"] == 0.3 and cell["cache_read_share"] == 0.8
    assert report["summary"]["verifier_cost_usd"] == 0.3 and report["summary"]["cache_read_share"] == 0.8
    assert "independent checks $0.30" in analysis.text(report) and "cache read 80%" in analysis.text(report)
