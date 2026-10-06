from __future__ import annotations

import pytest

from cauce import orchestrate, stops
from cauce.escalate import Failure
from cauce.launch import WorkerResult
from cauce.orchestrate import Options, run
from cauce.store import Store

from .test_orchestrate import Script, bad, kind, refused


def _stop(store: Store, task_id: int) -> stops.Stop:
    return stops.read(store.last_event(task_id, "finished")["data"]["stop"])


@pytest.mark.parametrize(
    ("results", "options", "cause", "who"),
    [
        ([refused()], Options(), "permission", "settings"),
        ([bad(Failure.ENVIRONMENT), bad(Failure.ENVIRONMENT)], Options(), "environment", "environment"),
        ([bad(Failure.CODE_BUG, cost=0.6)], Options(budget_usd=0.5), "budget", "budget"),
        ([bad(Failure.SPEC_BUG, "the API has no such field")], Options(), "spec", "worker"),
        ([bad(Failure.TURNS_EXHAUSTED)] * 2, Options(), "turns", "cauce"),
        ([bad(Failure.CODE_BUG, "still broken")] * 3, Options(), "ladder", "cauce"),
        ([bad(Failure.CODE_BUG, s) for s in ("a", "b")], Options(max_attempts=2), "attempts", "cauce"),
        ([bad(Failure.CODE_BUG, s) for s in ("missed a", "broke b", "left c", "forgot d")], Options(),
         "exhausted", "cauce"),
    ],
)
def test_every_ending_short_of_a_pass_says_why_and_who(git_repo, store, results, options, cause, who):
    report = run("do the thing", git_repo, store, options, registry={}, launcher=Script(*results),
                 classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.cause == cause and stop.who == who and stop.status == report.status
    assert stop.reason and stop.attempt == len(report.cells)
    assert report.stop.data() == stop.data()
    assert f"stopped by {stops.WHO[who]}" in report.text()


def test_a_refusal_names_the_rules_and_the_command_that_allows_them(git_repo, store):
    report = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                 classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.denied == ("Bash(node server.js)",)
    assert stop.account == "created the routes; node was refused"
    assert stops.next_step(report.task_id, stop) == f"cauce resume {report.task_id} --allow 'Bash(node:*)'"
    assert f"  cauce resume {report.task_id} --allow 'Bash(node:*)'" in report.text()


def test_approval_and_budget_say_how_to_go_on(tmp_path, store):
    script = Script(bad(Failure.INCONCLUSIVE, "could not read the queue"),
                    bad(Failure.INCONCLUSIVE, "the scheduler is unclear"))
    report = run("design it", tmp_path, store, registry={}, launcher=script, classifier=kind("plan"))
    stop = _stop(store, report.task_id)
    assert stop.cause == "approval" and stop.next_cell == "fable/high"
    assert stops.next_step(report.task_id, stop).endswith("--allow-approval")
    budget = stops.Stop("budget", "spent", budget_usd=2.5)
    assert stops.next_step(7, budget) == "cauce resume 7 --budget 5"
    assert stops.next_step(7, stops.Stop("spec", "wrong")) is None
    assert stops.next_step(7, stops.Stop("cancelled", "x"), resumable=False) is None


@pytest.mark.parametrize(("via", "how"), [("ui", "from the UI"), ("cli", "with `cauce cancel`")])
def test_a_cancel_says_a_person_did_it_and_how(git_repo, store, via, how):
    def launcher(spec):
        store.request_cancel(store.list_tasks()[0]["id"], via=via)
        return bad(Failure.CODE_BUG, "first")

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.who == "you" and how in stop.reason and stop.extra == {"via": via}
    assert store.get_task(report.task_id)["result"] == stop.reason


def test_a_signal_nobody_asked_for_and_ctrl_c_are_told_apart(git_repo, store):
    def killed(spec):
        raise orchestrate.Cancelled

    report = run("x", git_repo, store, registry={}, launcher=killed, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.cause == "signal" and stop.who == "outside" and stop.status == "cancelled"

    def interrupted(spec):
        raise KeyboardInterrupt

    report = run("y", git_repo, store, registry={}, launcher=interrupted, classifier=kind("implement"))
    stop = _stop(store, report.task_id)
    assert stop.who == "you" and "Ctrl-C" in stop.reason


def test_an_error_in_cauce_itself_is_its_own_account(git_repo, store):
    def broken(spec):
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run("x", git_repo, store, registry={}, launcher=broken, classifier=kind("implement"))
    task = store.list_tasks()[0]
    stop = _stop(store, task["id"])
    assert task["status"] == "failed" and stop.cause == "crashed"
    assert "RuntimeError: disk full" in stop.reason and task["result"] == stop.reason


def test_a_pass_has_no_account(git_repo, store):
    report = run("x", git_repo, store, registry={}, launcher=Script(WorkerResult(True, None, "ok", "1 passed")),
                 classifier=kind("implement"))
    assert report.stop is None and store.last_event(report.task_id, "finished")["data"]["stop"] is None
    assert stops.of(store, store.get_task(report.task_id)) is None


def test_older_tasks_get_an_account_read_back_from_their_records(store: Store):
    blocked = store.create_task("routes", status="blocked", source="cauce", cwd="/x", result="refused node")
    store.add_attempt(blocked["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission")
    store.set_move(blocked["id"], 1, "blocked", "the worker was refused Bash(node x.js)")
    store.add_event(blocked["id"], "attempt_finished", seq=1, denied=["Bash(node x.js)"])
    stop = stops.of(store, store.get_task(blocked["id"]))
    assert stop.recovered and stop.cause == "permission" and stop.denied == ("Bash(node x.js)",)
    assert stop.reason == "the worker was refused Bash(node x.js)"

    cases = [
        ("blocked", "the $5.00 budget is spent", None, "budget"),
        ("blocked", "its directory no longer exists", None, "missing_dir"),
        ("blocked", "the environment failed twice", "environment", "environment"),
        ("needs_approval", "", "code_bug", "approval"),
        ("replan", "", "spec_bug", "spec"),
        ("replan", "", "turns_exhausted", "turns"),
        ("replan", "", "code_bug", "ladder"),
        ("failed", "6 attempts without a pass", "code_bug", "attempts"),
        ("failed", "", "code_bug", "exhausted"),
        ("cancelled", "cancelled before it finished", None, "cancelled"),
        ("interrupted", "", None, "died"),
    ]
    for status, result, failure, cause in cases:
        t = store.create_task("t", status=status, source="cauce", result=result)
        if failure:
            store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure=failure)
        assert stops.of(store, store.get_task(t["id"])).cause == cause, status
    assert stops.of(store, store.create_task("t", status="done", source="cauce")) is None
    assert stops.recovered("weird") is None
    cancelled = stops.of(store, store.list_tasks(status=["cancelled"])[0])
    assert cancelled.who == "unknown" and stops.WHO[cancelled.who] == "not recorded"


def test_a_recorded_account_that_no_longer_matches_the_status_is_read_again(store: Store):
    t = store.create_task("t", status="running", source="cauce")
    stops.record(store, t["id"], stops.Stop("died", "gone"))
    assert stops.of(store, store.get_task(t["id"])).cause == "died"
    store.update_task(t["id"], status="cancelled")  # changed after, without an account
    assert stops.of(store, store.get_task(t["id"])).recovered


def test_the_view_says_who_in_words_and_what_to_do(store: Store):
    ran = store.create_task("ran", status="running", source="cauce", cwd="/x")
    stops.record(store, ran["id"], stops.Stop("permission", "refused", denied=("Bash(ls -la)",)))
    view = stops.view(store, store.get_task(ran["id"]))
    assert view["who"] == "your permission settings" and view["status"] == "blocked"
    assert view["next"] == f"cauce resume {ran['id']} --allow 'Bash(ls:*)'" and "allow" in view["todo"]
    never = store.enqueue("never ran", repo="r", cwd="/x")
    stops.record(store, never["id"], stops.Stop("cancelled", "you cancelled it", by="you"))
    view = stops.view(store, store.get_task(never["id"]))
    assert view["next"] is None and view["todo"].startswith("it never ran")
    assert stops.read(None) is None and stops.read({"reason": "no cause"}) is None
    assert stops.what_to_do(stops.Stop("mystery", "x")) == "read the attempts"
    assert stops.Stop("mystery", "x").status == "failed"
