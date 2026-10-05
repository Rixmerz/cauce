from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cauce import flow
from cauce.escalate import Failure
from cauce.launch import WorkerResult
from cauce.orchestrate import Options, run
from cauce.store import Store

from .test_orchestrate import bad, kind, ok


def _runner(results):
    results = list(results)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=lambda spec: results.pop(0),
                   classifier=kind("implement"), adapters=[], **kw)

    return runner


def test_a_lane_runs_in_order_and_pauses_on_the_first_failure(git_repo, store: Store):
    key = str(git_repo.resolve())
    for text in ("first", "second", "third"):
        store.enqueue(text, repo=key, cwd=str(git_repo))
    report = flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be done")]))
    assert [r.status for r in report.ran] == ["done", "replan"]
    assert "paused" in report.stopped_because or "nothing runnable" in report.stopped_because
    lane = store.lanes()[0]
    assert lane["paused"] and "replan" in lane["reason"] and lane["queued"] == 1
    first = store.list_tasks(status=["done"])[0]
    assert first["title"] == "first" and first["source"] == "cauce" and first["dispatched"] == 1
    assert next(m["role"] for m in store.messages(first["id"])) == "user"  # the queued text kept its id

    store.unpause_lane(key)
    again = flow.work(store, runner=_runner([ok()]))
    assert [r.status for r in again.ran] == ["done"] and not store.lanes()[0]["paused"]


def test_the_unattended_limit_and_a_vanished_directory(git_repo, store: Store, tmp_path):
    key = str(git_repo.resolve())
    for i in range(3):
        store.enqueue(f"t{i}", repo=key, cwd=str(git_repo))
    report = flow.work(store, max_tasks=2, runner=_runner([ok(), ok()]))
    assert len(report.ran) == 2 and "unattended limit" in report.stopped_because

    gone = store.enqueue("x", repo="elsewhere", cwd=str(tmp_path / "deleted"))
    flow.work(store, repo="elsewhere", runner=_runner([]))
    assert store.get_task(gone["id"])["status"] == "blocked"
    assert any(lane["repo"] == "elsewhere" and lane["paused"] for lane in store.lanes())


def test_a_busy_lane_waits_and_a_claim_happens_once(store: Store):
    a = store.enqueue("a", repo="r", cwd="/x")
    b = store.enqueue("b", repo="r", cwd="/x")
    other = store.enqueue("c", repo="other", cwd="/x")
    assert store.next_queued()["id"] == a["id"]
    assert store.claim(a["id"]) and not store.claim(a["id"])
    assert store.next_queued("r") is None  # serial: a is running from the dispatcher
    assert store.next_queued()["id"] == other["id"]
    store.update_task(a["id"], status="done")
    assert store.next_queued("r")["id"] == b["id"]


def test_queued_options_reach_the_run():
    task = {"options": '{"budget_usd": 1.5, "verify": "pytest", "kind": "docs", "start": "sonnet/high"}'}
    options = flow.queued_options(task, Options(max_attempts=3))
    assert (options.budget_usd, options.verify, options.kind, options.start.label, options.max_attempts) == (
        1.5, "pytest", "docs", "sonnet/high", 3)
    assert flow.queued_options({"options": "{}"}, Options()).verify is None


def test_the_sweep_marks_dead_runs_and_abandoned_prompts(store: Store):
    dead = store.create_task("dead", status="running", source="cauce", pid=999999, repo="r", dispatched=1)
    live = store.create_task("live", status="running", source="cauce", pid=1, repo="r")
    prompt = store.create_task("old prompt", status="running", source="hook", session_id="s")
    child = store.create_task("child", status="running", source="delegation", session_id="s")
    later = datetime.now(UTC) + timedelta(hours=13)
    swept = flow.sweep(store, now=later, is_alive=lambda pid: pid == 1)
    assert set(swept) == {dead["id"], prompt["id"]}
    assert store.get_task(live["id"])["status"] == "running"
    assert store.get_task(child["id"])["status"] == "running"
    assert store.lanes()[0]["paused"]
    assert flow.alive(None) is False and flow.alive(1) is True and flow.alive(999999) is False


def test_pinned_runs_never_teach_the_router(git_repo, store: Store):
    from cauce.matrix import Cell

    for _ in range(3):
        run("x", git_repo, store, Options(start=Cell("opus", "max")), registry={},
            launcher=lambda spec: WorkerResult(True, None, "ok", "1 passed"), classifier=kind("implement"),
            adapters=[])
    assert store.landings("implement") == []
    assert store.list_tasks()[0]["pinned"] == 1
