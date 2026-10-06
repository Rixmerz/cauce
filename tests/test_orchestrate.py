from __future__ import annotations

import json
from pathlib import Path

import pytest

from cauce import capabilities as caps
from cauce import orchestrate
from cauce.classify import Classification
from cauce.escalate import Failure
from cauce.launch import WRITE_TOOLS, WorkerResult
from cauce.matrix import LADDERS, Cell
from cauce.orchestrate import Options, choose_start, run

from .conftest import git

IMPLEMENT = LADDERS["implement"]


class Script:
    """A launcher that plays back results and records every spec it was given."""

    def __init__(self, *results: WorkerResult, write: str | None = None):
        self.results = list(results)
        self.specs = []
        self.write = write

    def __call__(self, spec):
        self.specs.append(spec)
        if self.write:
            (spec.target_dir / self.write).write_text(f"attempt {len(self.specs)}\n")
        return self.results.pop(0)


def ok(summary="done", cost=0.1) -> WorkerResult:
    return WorkerResult(True, None, summary, evidence="1 passed", cost_usd=cost)


def bad(failure: Failure, summary="still broken", cost=0.1) -> WorkerResult:
    return WorkerResult(False, failure, summary, cost_usd=cost)


def kind(name: str, complexity: str = "medium"):
    return lambda text: Classification(name, complexity, "rule", "test")


def test_choose_start_rises_with_complexity_and_history_never_falls():
    assert choose_start(IMPLEMENT, "medium", [], [])[0] == Cell("sonnet", "medium")
    assert choose_start(IMPLEMENT, "high", [], [])[0] == Cell("sonnet", "high")
    start, reasons = choose_start(IMPLEMENT, "medium", ["sonnet/xhigh", "sonnet/xhigh", "opus/high"], [])
    assert start == Cell("sonnet", "xhigh") and "this repository" in reasons[-1]
    start, reasons = choose_start(IMPLEMENT, "medium", ["sonnet/xhigh"], ["opus/high"] * 5)
    assert start == Cell("opus", "high") and "all repositories" in reasons[-1]
    assert choose_start(IMPLEMENT, "high", ["sonnet/medium"] * 4, [])[0] == Cell("sonnet", "high")
    # a landing off the ladder counts at the highest rung it is not below
    assert choose_start(IMPLEMENT, "medium", ["opus/max"] * 3, [])[0] == Cell("opus", "high")
    assert choose_start(IMPLEMENT, "medium", ["haiku"] * 3, [])[0] == Cell("sonnet", "medium")


def test_the_model_that_served_is_recorded_and_a_pin_reaches_the_worker(git_repo, store, monkeypatch):
    from cauce import models

    served = WorkerResult(True, None, "done", "1 passed", served_model="claude-sonnet-5")
    report = run("x", git_repo, store, registry={}, launcher=Script(served), classifier=kind("implement"))
    assert report.served == ["claude-sonnet-5"] and "attempt 1 sonnet/medium [claude-sonnet-5]" in report.text()
    assert store.attempts(report.task_id)[0]["served_model"] == "claude-sonnet-5"
    assert store.last_event(report.task_id, "attempt_finished")["data"]["served_model"] == "claude-sonnet-5"
    # the person's sessions run a newer sonnet: the plan says so, with the pin
    store.add_usage(message_id="m1", session_id="s", model="claude-sonnet-5-5", output_tokens=1, ts="2099-01-01")
    dry = run("y", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert any("workers last ran claude-sonnet-5 for `sonnet` while your sessions use claude-sonnet-5-5" in r
               and "cauce config model sonnet claude-sonnet-5-5" in r for r in dry.plan.reasons)
    models.pin("sonnet", "claude-sonnet-5-5")
    script = Script(ok())
    run("z", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert script.specs[0].model_id == "claude-sonnet-5-5"
    dry = run("w", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert not any("workers last ran" in r for r in dry.plan.reasons)


def test_a_first_try_pass_leaves_a_branch(git_repo, store):
    script = Script(ok(), write="feature.py")
    report = run("add the feature", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.final_cell == "sonnet/medium"
    assert report.branch == f"cauce/task-{report.task_id}"
    assert "feature.py" in git(git_repo, "show", "--name-only", report.branch)
    assert not (git_repo / "feature.py").exists()
    spec = script.specs[0]
    assert spec.launch_dir != git_repo and spec.workdir is None
    assert spec.max_budget_usd == 5.0 and spec.disallowed_tools == ()
    task = store.get_task(report.task_id)
    assert task["status"] == "done" and task["final_cell"] == "sonnet/medium"
    assert store.messages(report.task_id)[-1]["role"] == "worker"
    assert "passed at sonnet/medium" in report.text()


def test_shallow_failures_climb_effort_and_keep_the_work(git_repo, store):
    seen = []

    def launcher(spec):
        target = spec.target_dir / "f.py"
        seen.append(target.read_text() if target.exists() else None)
        target.write_text(f"attempt {len(seen)}\n")
        return bad(Failure.CODE_BUG, "missed the empty case") if len(seen) == 1 else ok("fixed")

    script = Script(bad(Failure.CODE_BUG, "missed the empty case"), ok("fixed"))
    report = run("fix the parser", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.cells == ["sonnet/medium", "sonnet/high"]
    assert report.moves[0].startswith("more_effort to sonnet/high: ")
    moved = store.last_event(report.task_id, "moved")["data"]
    assert moved["seq"] == 1 and moved["from_cell"] == "sonnet/medium" and moved["to_cell"] == "sonnet/high"
    assert moved["axis"] == "effort" and moved["trigger"] == "code_bug" and moved["turns_to"] == 30
    assert moved["because"][-1].startswith("next: sonnet/high") and moved["budget_left"] < 5
    assert "    · next: sonnet/high (the floor for work that needs judgment)" in report.text()
    assert seen == [None, "attempt 1\n"]  # the more thorough attempt continues the work
    run("fix the parser", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    second = script.specs[1]
    assert "missed the empty case" in second.prompt and "already tried" in second.prompt
    # the failure and the fix that worked are now memory
    dead = store.dead_ends("fix the parser")
    assert dead and dead[0]["worked_instead"].startswith("sonnet/high")


def test_a_wrong_approach_resets_the_worktree_and_changes_model(git_repo, store):
    seen = []

    def launcher(spec):
        seen.append(sorted(p.name for p in spec.target_dir.iterdir() if p.name != ".git"))
        (spec.target_dir / "wrong.py").write_text("x\n")
        return bad(Failure.APPROACH, "rewrote the wrong module") if len(seen) == 1 else ok()

    report = run("refactor auth flow", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.cells == ["sonnet/medium", "opus/high"]
    assert seen[1] == ["app.py"]  # the second model started from the base commit


def test_read_only_work_gets_no_edit_tools_and_no_worktree(git_repo, store):
    script = Script(ok("found it"))
    report = run("where is the router defined", git_repo, store, registry={}, launcher=script,
                 classifier=kind("explore"))
    spec = script.specs[0]
    assert spec.disallowed_tools == WRITE_TOOLS
    assert spec.launch_dir == git_repo.resolve()
    assert "read-only" in spec.prompt
    assert report.branch is None and report.final_cell == "haiku"


def test_capabilities_are_handed_over_and_reactive_ones_join_after_a_failure(git_repo, store):
    registry = {
        "index": caps.Capability("index", {"command": "idx", "args": ["{workdir}"]}, hint="use it"),
        "inspector": caps.Capability("inspector", {"command": "li"}, when=(), after_failure=("implement",)),
    }
    script = Script(bad(Failure.CODE_BUG), ok())
    run("polish it", git_repo, store, registry=registry, launcher=script, classifier=kind("implement"))
    first, second = script.specs
    assert set(first.mcp_servers) == {"index"} and set(second.mcp_servers) == {"index", "inspector"}
    assert first.mcp_servers["index"]["args"] == [str(first.target_dir)]
    assert "index: use it" in first.append_system_prompt


def test_a_claimed_pass_the_check_rejects_is_a_failure(git_repo, store):
    prompts = []

    def launcher(spec):
        prompts.append(spec.prompt)
        if len(prompts) == 2:
            (spec.target_dir / "proof.txt").write_text("ok\n")
        return ok("all good")

    report = run("make it pass", git_repo, store, Options(verify="test -f proof.txt"), registry={},
                 launcher=launcher, classifier=kind("implement"))
    assert report.status == "done"
    assert report.cells == ["sonnet/medium", "sonnet/high"]
    attempt = store.attempts(report.task_id)[0]
    assert attempt["failure"] == "code_bug" and "exited 1" in attempt["summary"]
    assert "checked by running: test -f proof.txt" in prompts[0]


def test_a_check_that_passes_keeps_the_pass(git_repo, store):
    script = Script(ok())
    report = run("x", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"


@pytest.mark.parametrize(
    ("results", "status"),
    [
        ([bad(Failure.SPEC_BUG, "the API has no such field")], "replan"),
        ([bad(Failure.ENVIRONMENT), bad(Failure.ENVIRONMENT)], "blocked"),
        ([bad(Failure.CODE_BUG, s) for s in ("missed a", "broke b", "left c", "forgot d")], "failed"),
        ([bad(Failure.CODE_BUG, "still broken")] * 3, "replan"),  # every model says the same
    ],
)
def test_runs_end_with_a_status_that_says_why(git_repo, store, results, status):
    report = run("do the thing", git_repo, store, registry={}, launcher=Script(*results),
                 classifier=kind("implement"))
    assert report.status == status
    assert store.get_task(report.task_id)["status"] == status
    assert report.branch is None
    assert results[-1].summary in report.summary or report.summary.endswith("attempts without a pass")


def test_fable_waits_for_approval(tmp_path, store):
    script = Script(bad(Failure.INCONCLUSIVE, "could not read the queue"),
                    bad(Failure.INCONCLUSIVE, "the scheduler is unclear"))
    report = run("design the system", tmp_path, store, registry={}, launcher=script, classifier=kind("plan"))
    assert report.status == "needs_approval" and report.cells == ["opus/xhigh", "opus/max"]


def test_budget_and_attempt_ceilings(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, cost=0.6), ok())
    report = run("x", git_repo, store, Options(budget_usd=0.5), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "blocked" and "budget" in report.summary
    script = Script(bad(Failure.CODE_BUG, "a"), bad(Failure.CODE_BUG, "b"))
    report = run("y", git_repo, store, Options(max_attempts=2), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "failed" and "2 attempts" in report.summary


def test_the_second_attempt_may_spend_only_what_is_left(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, cost=1.25), ok())
    run("x", git_repo, store, Options(budget_usd=3), registry={}, launcher=script, classifier=kind("implement"))
    assert script.specs[1].max_budget_usd == 1.75


def test_history_moves_the_next_start(git_repo, store):
    for _ in range(3):
        script = Script(bad(Failure.CODE_BUG, "a"), bad(Failure.CODE_BUG, "b"), ok(), write="f.py")
        run("tweak the layout", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    script = Script(ok(), write="f.py")
    report = run("tweak the layout again", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.cells == ["sonnet/xhigh"]
    assert any("passed at sonnet/xhigh" in r for r in report.plan.reasons)


def test_dead_ends_from_another_repository_reach_the_brief(git_repo, store):
    p = store.open_problem("websocket reconnect loop", repo="github.com/x/other")
    store.add_fix(p, "raise the backoff", "failed", repo="github.com/x/other", why="the loop is in the client")
    script = Script(ok())
    run("the websocket reconnect loop is back", git_repo, store, registry={}, launcher=script,
        classifier=kind("implement"))
    assert "raise the backoff" in script.specs[0].prompt and "github.com/x/other" in script.specs[0].prompt
    task_id = store.list_tasks()[0]["id"]
    assert store.last_event(task_id, "dead_ends")["data"]["shown"][0]["tried"] == "raise the backoff"


def test_options_pin_and_launch_dir_and_dry_run(git_repo, store, tmp_path):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    script = Script(ok())
    report = run("x", git_repo, store, Options(start=Cell("opus", "low"), launch_dir=launch_dir, isolate=False),
                 registry={}, launcher=script, classifier=kind("implement"))
    spec = script.specs[0]
    assert spec.cell == Cell("opus", "low") and spec.launch_dir == launch_dir.resolve()
    assert spec.workdir == git_repo.resolve()
    assert report.plan.reasons == ["pinned by the caller"]
    dry = run("y", git_repo, store, Options(dry_run=True, kind="docs"), registry={}, launcher=script)
    assert dry.task_id is None and dry.status == "dry run" and dry.plan.start == Cell("haiku")
    assert "plan: dry run" in dry.text()


def test_rules_only_classification(git_repo, store):
    plan = orchestrate.plan("haz commit y push", Path(git_repo), store, Options(use_model_classifier=False), {})
    assert plan.kind == "docs" and plan.start == Cell("haiku")


# --- livespec, adopted ---------------------------------------------------------

def _livespec(tmp_path, *, runner=None, popen=None):
    import subprocess as sp

    from cauce.adapters.livespec import Livespec

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "uvx").write_text("#!/bin/sh\n")
    (bindir / "uvx").chmod(0o755)
    env = {"PATH": str(bindir), "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
           "CAUCE_HOME": str(tmp_path / "cauce")}
    return Livespec(env=env, runner=runner or (lambda argv, **kw: sp.CompletedProcess(argv, 0, "", "")),
                    **({"popen": popen} if popen else {}))


def test_livespec_runs_through_the_whole_flow(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)  # dated in the future, so never older than the last commit
    script = Script(ok("fixed"))

    def launcher(spec):
        (spec.target_dir / "src").mkdir(exist_ok=True)
        (spec.target_dir / "src" / "billing.py").write_text("fixed\n")
        script(spec)
        return WorkerResult(True, None, "fixed", "1 passed", ("src/billing.py",), 0.1)

    report = run("charge_card charges twice on retry", git_repo, store, registry={}, launcher=launcher,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path)])
    spec = script.specs[0]
    # routing: a critical spec moves the start one rung up
    assert report.cells == ["sonnet/high"]
    assert any("critical spec" in r for r in report.plan.reasons)
    # briefing: the code map opens the worker's prompt
    assert "Code map from livespec" in spec.prompt and "SPEC-7" in spec.prompt
    # the worker gets livespec's server and a hint naming the workspace
    assert spec.mcp_servers["livespec"]["command"] == "uvx"
    # and its tools are allowed with it: a worker is never refused the index it was handed
    assert "mcp__livespec" in spec.allowed_tools
    assert f'workspace="{git_repo.resolve()}"' in spec.append_system_prompt
    assert "livespec" in report.plan.capabilities
    # after the pass: what the change touched
    assert any("SPEC-7" in line for line in report.impact)
    assert "livespec: present" in report.text()


def test_an_unindexed_repo_is_indexed_in_the_background_and_the_task_starts_at_once(git_repo, store, tmp_path):
    from cauce.adapters import livespec as ls

    from .livespec_fixture import build

    started = []

    def popen(argv, **kw):
        started.append(argv)
        return None

    dry = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
              classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert "livespec: would index in the background" in dry.plan.reasons
    assert started == [] and not (git_repo / ".mcp-docs").exists()
    script = Script(ok())
    lines = []
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)], progress=lines.append)
    # the index is started, never waited on: the task runs without it
    assert started and started[0][-2:] == ["index-livespec", str(git_repo.resolve())]
    assert "livespec: indexing in the background; this task runs without it" in report.plan.reasons
    assert "Code map from livespec" not in script.specs[0].prompt
    assert report.status == "done" and lines[0].startswith(f"cauce: task #{report.task_id} started")
    # a task planned while that index runs starts no second one
    adapter = _livespec(tmp_path, popen=popen)
    with ls._lock(adapter.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        busy = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=Script(ok()),
                   classifier=kind("implement"), adapters=[adapter])
    assert "livespec: already indexing in the background; this task runs without it" in busy.plan.reasons
    assert len(started) == 1
    # the next task finds the index the background run built
    build(git_repo)
    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[_livespec(tmp_path, popen=popen)])
    assert len(started) == 1 and "22 caller(s)" in script.specs[0].prompt


def test_a_stale_index_is_used_as_it_is_while_one_refresh_runs(git_repo, store, tmp_path, monkeypatch):
    from cauce.adapters import livespec as ls

    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setattr(ls, "_older_than_head", lambda indexed_at, root: True)
    started = []
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: started.append(argv))
    script = Script(ok())
    report = run("parse_amount rounds wrong", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), adapters=[adapter])
    assert "livespec: refreshing in the background; this task uses the index as it is" in report.plan.reasons
    assert "22 caller(s)" in script.specs[0].prompt and len(started) == 1
    # a refresh that holds the lock is not started twice
    with ls._lock(adapter.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        again = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=False), registry={},
                    launcher=Script(ok()), classifier=kind("implement"), adapters=[adapter])
    assert "livespec: already refreshing in the background; this task uses the index as it is" in again.plan.reasons
    assert len(started) == 1


def test_a_stale_index_livespec_cannot_refresh_says_so_and_never_claims_a_running_one(
        git_repo, store, tmp_path, monkeypatch):
    from cauce.adapters import livespec as ls

    from .livespec_fixture import build

    build(git_repo)
    monkeypatch.setattr(ls, "_older_than_head", lambda indexed_at, root: True)
    adapter = _livespec(tmp_path, popen=lambda argv, **kw: pytest.fail("nothing to start"))
    monkeypatch.setattr(adapter, "server", lambda: None)
    report = run("parse_amount rounds wrong", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("implement"), adapters=[adapter])
    assert "livespec: cannot refresh here; this task uses the index as it is" in report.plan.reasons
    assert not any("already" in r or "would refresh" in r for r in report.plan.reasons)


def test_review_of_critical_code_is_reviewed_as_critical(git_repo, store, tmp_path):
    from .livespec_fixture import build

    build(git_repo)
    report = run("review the charge_card change", git_repo, store, Options(dry_run=True), registry={},
                 classifier=kind("review-routine"), adapters=[_livespec(tmp_path)])
    assert report.plan.kind == "review-critical"
    assert "Code map from livespec" in report.text()


def test_the_switch_turns_livespec_off_everywhere(git_repo, store, monkeypatch):
    from cauce import orchestrate as o

    monkeypatch.setenv("CAUCE_LIVESPEC", "on")
    assert [a.name for a in o.adopted(Options(), None)] == ["livespec"]
    assert o.adopted(Options(livespec=False), None) == []
    monkeypatch.setenv("CAUCE_LIVESPEC", "off")
    assert o.adopted(Options(), None) == []
    assert [a.name for a in o.adopted(Options(livespec=True), None)] == ["livespec"]


# --- what a watcher sees, and stopping a run -------------------------------------

def test_a_run_leaves_an_event_trail_a_watcher_can_follow(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, "missed it"), ok("fixed"))
    report = run("fix the thing", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    events = store.events(task_id=report.task_id)
    assert [e["kind"] for e in events] == ["planned", "attempt_started", "attempt_finished", "moved",
                                           "attempt_started", "attempt_finished", "finished"]
    assert events[1]["data"]["cell"] == "sonnet/medium" and events[3]["data"]["next_cell"] == "sonnet/high"
    assert events[-1]["data"]["status"] == "done"
    assert store.events(after=events[2]["id"], task_id=report.task_id)[0]["kind"] == "moved"
    task = store.get_task(report.task_id)
    assert task["pid"] is None and task["current_cell"] is None


def test_a_cancel_requested_between_attempts_stops_the_run(git_repo, store):
    def launcher(spec):
        store.request_cancel(store.list_tasks()[0]["id"])
        return bad(Failure.CODE_BUG, "first")

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "cancelled" and report.cells == ["sonnet/medium"]
    assert report.branch is None
    assert store.get_task(report.task_id)["status"] == "cancelled"


def test_a_cancel_in_the_middle_of_an_attempt_cleans_up(git_repo, store):
    from cauce.orchestrate import Cancelled

    def launcher(spec):
        (spec.target_dir / "half.py").write_text("x\n")
        raise Cancelled

    report = run("x", git_repo, store, registry={}, launcher=launcher, classifier=kind("implement"))
    assert report.status == "cancelled" and report.branch is None
    assert "cauce/task-" not in git(git_repo, "branch", "--list")


def test_the_fix_that_worked_is_anchored_to_its_commit_and_costs_inform_the_next_plan(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, "missed it"), ok("fixed", cost=0.4), write="f.py")
    report = run("fix the parser", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    fix = store.problem(store.dead_ends("fix the parser")[0]["problem_id"])["fixes"][-1]
    assert fix["outcome"] == "worked" and fix["commit_sha"] == git(git_repo, "rev-parse", report.branch)
    nxt = run("another", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert any("cost $0.50 on average" in r for r in nxt.plan.reasons)


def refused(summary="created the routes; node was refused", verdict="inconclusive", cost=0.1) -> WorkerResult:
    return WorkerResult(False, Failure.PERMISSION, summary, cost_usd=cost, verdict=verdict,
                        denied=("Bash(node server.js)",), changed_paths=("routes.js",))


def test_work_a_refusal_stopped_is_kept_unverified_and_the_report_says_what_git_saw(git_repo, store):
    """The audited run: a worker wrote the routes, was refused `node`, said so; the
    run ended blocked and the routes were nowhere. Now they are on the branch."""
    script = Script(refused(), write="routes.js")
    report = run("create the training routes", git_repo, store, Options(allow_tools=("Bash(npm run build)",)),
                 registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and report.cells == ["sonnet/medium"]  # no retry into the same refusal
    assert "Bash(node server.js)" in report.summary and "--allow" in report.summary
    assert "The last worker's own account: created the routes" in report.summary
    assert script.specs[0].allowed_tools == ("Bash(npm run build)",)
    assert report.branch == f"cauce/task-{report.task_id}" and report.changed == ["routes.js"]
    assert "routes.js" in git(git_repo, "show", "--name-only", report.branch)
    assert "(unverified, blocked)" in git(git_repo, "log", "-1", "--format=%s", report.branch)
    text = report.text()
    assert "changed (from git): routes.js" in text
    assert "keeps this task's unverified work" in text and f"cauce resume {report.task_id}" in text
    assert "review it, then merge it" not in text
    assert json.loads(store.get_task(report.task_id)["options"])["allow_tools"] == ["Bash(npm run build)"]
    finished = store.last_event(report.task_id, "finished")["data"]
    assert finished["changed"] == ["routes.js"] and finished["branch"] == report.branch
    assert store.last_event(report.task_id, "attempt_finished")["data"]["denied"] == ["Bash(node server.js)"]


def test_the_repositorys_check_can_settle_what_a_refused_worker_could_not(git_repo, store):
    script = Script(refused(), write="routes.js")
    report = run("create the routes", git_repo, store, Options(verify="test -f routes.js"), registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.final_cell == "sonnet/medium"
    assert "`test -f routes.js` passed in its place" in report.summary
    assert store.attempts(report.task_id)[0]["passed"] == 1
    # red, it stays blocked, with the check's output for the person
    script = Script(refused(), write="routes.js")
    report = run("create the routes", git_repo, store, Options(verify="echo nope; exit 3"), registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "blocked"
    assert "exited 3" in store.attempts(report.task_id)[0]["evidence"]
    # a worker that called it a failure is not passed by a green check, nor a task that wrote nothing
    for result in (refused(verdict="fail"), WorkerResult(False, Failure.PERMISSION, "x", verdict="inconclusive",
                                                         denied=("Bash(ls)",))):
        report = run("create the routes", git_repo, store, Options(verify="true"), registry={},
                     launcher=Script(result, write="routes.js" if result.verdict == "fail" else None),
                     classifier=kind("implement"))
        assert report.status == "blocked"
    # outside a worktree, what the attempt changed is what the task wrote
    report = run("create the routes", git_repo, store, Options(verify="true", isolate=False), registry={},
                 launcher=Script(refused()), classifier=kind("implement"))
    assert report.status == "done" and report.changed == ["routes.js"]
    assert "these changes are in your checkout\n" in report.text()  # no branch to review: the files are the work
    report = run("create the routes", git_repo, store, Options(isolate=False), registry={},
                 launcher=Script(refused()), classifier=kind("implement"))
    assert "these changes are in your checkout, unverified" in report.text()


def test_work_that_failed_is_dropped_and_the_report_says_so(git_repo, store):
    script = Script(bad(Failure.SPEC_BUG, "no such API"), write="half.py")
    report = run("do it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "replan" and report.branch is None
    assert "changed (from git): half.py" in report.text() and "nothing of it was kept" in report.text()
    # a reading task prints no changed line: it has nothing to change
    report = run("where is it", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("explore"))
    assert "changed (from git)" not in report.text()


def test_a_resumed_task_continues_on_its_branch_with_its_history_in_the_brief(git_repo, store):
    first = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                classifier=kind("implement"))
    (git_repo / ".gitignore").write_text("node_modules/\n")
    (git_repo / "node_modules").mkdir()
    seen = []

    spec_dirs = []

    def launcher(spec):
        seen.append((spec.prompt, (spec.target_dir / "routes.js").read_text()))
        spec_dirs.extend(spec.extra_dirs)
        return ok("checked the routes")

    history = [orchestrate.Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "node was refused",
                                   ("Bash(node server.js)",))]
    report = run("create the routes", git_repo, store, Options(kind="implement", start=Cell("sonnet", "medium")),
                 registry={}, launcher=launcher, task_id=first.task_id, history=history)
    prompt, routes = seen[0]
    assert routes == "attempt 1\n"  # what the first run wrote is where the second starts
    assert "is resumed" in prompt and "attempt 1 [sonnet/medium, permission] node was refused" in prompt
    assert "refused: Bash(node server.js)" in prompt
    assert "Linked from the person's checkout and shared with it: node_modules" in prompt
    assert spec_dirs == [(git_repo / "node_modules").resolve()]
    assert report.status == "done" and report.branch == first.branch
    assert store.get_task(first.task_id)["cost_usd"] == pytest.approx(0.2)
    assert [a["seq"] for a in store.attempts(first.task_id)] == [1, 2]


def test_a_resumed_worker_that_finds_the_work_done_is_settled_by_the_check(git_repo, store):
    first = run("create the routes", git_repo, store, registry={}, launcher=Script(refused(), write="routes.js"),
                classifier=kind("implement"))
    again = WorkerResult(False, Failure.PERMISSION, "routes.js was already there", verdict="inconclusive",
                         denied=("Bash(node server.js)",))
    report = run("create the routes", git_repo, store, Options(kind="implement", verify="test -f routes.js"),
                 registry={}, launcher=Script(again), task_id=first.task_id)
    assert report.status == "done" and report.changed == ["routes.js"]
    assert "(unverified" in git(git_repo, "log", "-2", "--format=%s", report.branch).splitlines()[1]
    assert "(unverified" not in git(git_repo, "log", "-1", "--format=%s", report.branch)


def test_a_worker_that_ran_out_of_turns_is_checked_by_cauce_and_a_red_check_reaches_the_next_brief(git_repo, store):
    """A large change used every turn before it ran the tests; the tests were never run
    and the run was sent back to be split. The repository's check now runs for it."""
    out_of_turns = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 30 turns without a verdict", cost_usd=0.1)
    report = run("remove the old module", git_repo, store, Options(verify="test -f gone.txt"), registry={},
                 launcher=Script(out_of_turns, write="gone.txt"), classifier=kind("refactor"))
    assert report.status == "done" and report.cells == ["sonnet/medium"]
    assert "ran out of turns before checking its work; `test -f gone.txt` passed in its place" in report.summary

    specs = []

    def launcher(spec):
        specs.append(spec)
        (spec.target_dir / ("half.txt" if len(specs) == 1 else "done.txt")).write_text("x\n")
        return out_of_turns if len(specs) == 1 else ok()

    check = "test -f done.txt || { echo 2 tests failed; exit 1; }"
    report = run("remove the old module", git_repo, store, Options(verify=check), registry={}, launcher=launcher,
                 classifier=kind("refactor"))
    first = store.attempts(report.task_id)[0]
    assert first["failure"] == "turns_exhausted" and "2 tests failed" in first["evidence"]
    assert f"cauce ran `{check}`, which exited 1" in first["summary"]
    assert specs[1].max_turns == 60 and report.status == "done"
    assert "The output that failed the last attempt, last lines:" in specs[1].prompt
    assert "2 tests failed" in specs[1].prompt


def test_a_run_in_the_checkout_resumes_in_the_checkout(git_repo, store):
    assert Options(isolate=False).stored()["no_isolate"] is True
    assert "no_isolate" not in Options().stored()
    from cauce import flow

    task = {"options": '{"no_isolate": true}'}
    assert flow.queued_options(task, Options()).isolate is False
    assert flow.queued_options({"options": "{}"}, Options()).isolate is True


def test_the_plan_names_the_rules_workers_here_were_refused_before(git_repo, store, monkeypatch):
    for i in range(2):
        refusal = WorkerResult(False, Failure.PERMISSION, "refused", verdict="inconclusive",
                               denied=("Bash(npx tsc --noEmit; npm run build)",),
                               allow=("Bash(npx tsc:*)", "Bash(npm run build:*)"), permission_mode="auto")
        run(f"task {i}", git_repo, store, registry={}, launcher=Script(refusal), classifier=kind("implement"))
    old = store.create_task("older", status="blocked", source="cauce", repo=str(git_repo.resolve()))
    store.add_event(old["id"], "attempt_finished", seq=1, denied=["Bash(make lint)"])  # before rules were kept
    dry = run("next", git_repo, store, Options(dry_run=True, allow_tools=("Bash(npm run build:*)",)), registry={},
              classifier=kind("implement"))
    note = next(r for r in dry.plan.reasons if r.startswith("workers here were refused before"))
    assert "Bash(npx tsc:*) ×2" in note and "npm run build" not in note
    # what acceptEdits refused, before auto mode, is not what auto mode will refuse
    assert "make lint" not in note
    monkeypatch.setenv("CAUCE_MODE", "acceptEdits")
    dry = run("next", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    note = next(r for r in dry.plan.reasons if r.startswith("workers here were refused before"))
    assert note.startswith("workers here were refused before: Bash(make lint:*) ×1.")
    blocked = store.list_tasks(status=["blocked"])[-1]
    assert "--allow 'Bash(npx tsc:*)' --allow 'Bash(npm run build:*)'" in store.get_task(blocked["id"])["result"]


def test_the_task_text_says_who_wrote_it(git_repo, store):
    """Sent by the main session through its own Bash, the text is the orchestrator's,
    not something the person typed."""
    sent = run("x", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"), session_id="s1")
    typed = run("y", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"))
    assert store.messages(sent.task_id)[0]["role"] == "orchestrator"
    assert store.messages(typed.task_id)[0]["role"] == "person"


def test_the_plan_says_when_a_task_needs_a_browser_or_running_servers(git_repo, store, tmp_path):
    text = "Start both dev servers (app on 4200) and open the browser at localhost:4200 to check the admin panel"
    dry = run(text, git_repo, store, Options(dry_run=True), registry={}, classifier=kind("test"))
    notes = [r for r in dry.plan.reasons if r.startswith("this task needs")]
    assert len(notes) == 2 and "browser MCP server" in notes[0] and "--allow 'mcp__browser'" in notes[0]
    assert "start them yourself" in notes[1]
    (tmp_path / "caps.json").write_text(json.dumps({"browser": caps.EXAMPLE["browser"]}))
    registry = caps.load(tmp_path / "caps.json")
    dry = run(text, git_repo, store, Options(dry_run=True), registry=registry, classifier=kind("explore"))
    assert [r for r in dry.plan.reasons if r.startswith("this task needs")] == [notes[1]]
    plain = run("fix the parser", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("implement"))
    assert not any(r.startswith("this task needs") for r in plain.plan.reasons)


def test_work_that_outgrew_its_turns_is_kept_for_the_resume(git_repo, store):
    out = WorkerResult(False, Failure.TURNS_EXHAUSTED, "ran out", cost_usd=0.1)
    report = run("reduce it", git_repo, store, registry={}, launcher=Script(out, out, write="half.py"),
                 classifier=kind("refactor"))
    assert report.status == "replan" and report.stop.cause == "turns"
    assert report.branch == f"cauce/task-{report.task_id}"
    assert "half.py" in git(git_repo, "show", "--name-only", report.branch)
    assert f"cauce resume {report.task_id} --max-turns 120" in report.text()


def test_a_worker_gets_its_mode_the_repository_s_rules_and_the_tools_it_was_handed(git_repo, store, tmp_path):
    from cauce import grants, repo

    grants.grant(repo.key(git_repo), ["Bash(npm:*)"])
    (tmp_path / "caps.json").write_text(json.dumps({"browser": caps.EXAMPLE["browser"]}))
    script = Script(ok())
    registry = caps.load(tmp_path / "caps.json")
    run("check the page", git_repo, store, Options(allow_tools=("Bash(make:*)",)), registry=registry,
        launcher=script, classifier=kind("test"))
    spec = script.specs[0]
    assert spec.permission_mode == "auto"
    assert spec.allowed_tools == ("Bash(make:*)", "Bash(npm:*)", "mcp__browser")
    script = Script(ok())
    run("where is it", git_repo, store, registry={}, launcher=script, classifier=kind("explore"))
    assert script.specs[0].permission_mode == "bypassPermissions"  # a haiku cell
    dry = run("where is it", git_repo, store, Options(dry_run=True), registry={}, classifier=kind("explore"))
    assert "workers on haiku run in bypassPermissions mode" in dry.plan.reasons


def test_a_refused_attempt_tells_how_to_keep_its_rules(git_repo, store):
    report = run("x", git_repo, store, registry={}, launcher=Script(refused()), classifier=kind("implement"))
    assert "or, for every task in this repository: cauce allow 'Bash(node:*)'" in report.text()


def test_a_reading_task_is_told_its_findings_are_the_answer_and_stops_when_cells_agree(git_repo, store):
    said = "the API still allows the old role in two files"
    script = Script(bad(Failure.CODE_BUG, said), bad(Failure.CODE_BUG, said + "."))
    report = run("audit the role rename", git_repo, store, registry={}, launcher=script,
                 classifier=kind("review-critical"))
    assert "also when what you found is broken" in script.specs[0].prompt
    assert report.status == "failed" and report.cells == ["opus/high", "opus/xhigh"]
    assert report.stop.cause == "converged" and report.stop.account == said + "."
    assert "resuming would only repeat them" in report.text() and "cauce resume" not in report.text()


def test_a_passing_worker_s_learned_facts_are_filed_and_later_workers_read_them(git_repo, store, monkeypatch):
    from cauce import notes

    monkeypatch.setenv("CAUCE_NOTES", "on")
    learned = WorkerResult(True, None, "done", "1 passed", learned=("Prices are stored in cents in every table",))
    asked = []

    def ask(system, schema, question, **kw):
        asked.append(question)
        return {"notes": [{"fact": 0, "keep": True, "topic": "business", "title": "Prices in cents",
                           "duplicate_of": None, "links": [], "paths": ["prices.py"], "new_topic": None}]}, 0.0, ""

    report = run("store prices in cents", git_repo, store, registry={}, launcher=Script(learned, write="prices.py"),
                 classifier=kind("implement"), notes_ask=ask)
    assert report.noted == [{"id": 1, "new": True, "topic": "business", "title": "Prices in cents"}]
    assert "notes: kept #1 [business] Prices in cents" in report.text()
    assert "- prices.py" in asked[0]
    anchor = store.note_anchors([1])[0]
    assert anchor["path"] == "prices.py" and anchor["commit_sha"] == git(git_repo, "rev-parse", "cauce/task-1")
    assert store.last_event(report.task_id, "noted")["data"]["notes"][0]["id"] == 1
    # the next worker in this project reads it
    script = Script(ok())
    later = run("change how prices are stored in cents", git_repo, store, registry={}, launcher=script,
                classifier=kind("implement"), notes_ask=ask)
    assert "What this project's notes say" in script.specs[0].prompt and "#1 [business] Prices in cents" in (
        script.specs[0].prompt)
    assert any(r.startswith("notes: 1 from this project's memory") for r in later.plan.reasons)
    # off: neither read nor filed
    monkeypatch.setenv("CAUCE_NOTES", "off")
    script = Script(learned)
    run("prices in cents again", git_repo, store, registry={}, launcher=script, classifier=kind("implement"),
        notes_ask=ask)
    assert "notes say" not in script.specs[0].prompt and len(asked) == 1
    assert notes.recall(store, notes.repo.key(git_repo), "prices")[0]["id"] == 1


def test_notes_that_cannot_be_filed_never_fail_the_pass(git_repo, store, monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")

    def broken(*a, **kw):
        raise RuntimeError("database is locked")

    learned = WorkerResult(True, None, "done", "1 passed", learned=("The cache is warmed by a cron job",))
    report = run("x", git_repo, store, registry={}, launcher=Script(learned), classifier=kind("implement"),
                 notes_ask=broken)
    assert report.status == "done" and report.noted == []
    assert any("could not be filed (RuntimeError: database is locked)" in line for line in report.impact)
    # a failed attempt's facts are not filed: they were not confirmed by a pass
    report = run("y", git_repo, store, registry={}, launcher=Script(
        WorkerResult(False, Failure.SPEC_BUG, "no", learned=("A guess about the cache layer",))),
        classifier=kind("implement"), notes_ask=broken)
    assert report.status == "replan" and not store.notes(notes_key(git_repo))


def notes_key(where):
    from cauce import repo

    return repo.key(where)


# --- what a run says while it runs ------------------------------------------------

def test_a_run_says_its_task_number_warnings_and_attempts_as_they_happen(git_repo, store):
    lines = []
    script = Script(bad(Failure.CODE_BUG, "missed the rounding " + "x" * 400), ok("fixed"))
    report = run("check the checkout page in the browser and fix the total", git_repo, store, registry={},
                 launcher=script, classifier=kind("implement"), progress=lines.append)
    n = report.task_id
    assert lines[0] == f"cauce: task #{n} started: implement, at {report.plan.start.label}"
    # the warning comes before any money is spent, not only in the report
    assert lines[1].startswith(f"cauce: #{n} warning: this task needs a browser")
    assert lines.index(f"cauce: #{n} attempt 1 at {report.cells[0]}") == 2
    ended = lines[3]
    assert ended.startswith(f"cauce: #{n} attempt 1 ended code_bug: missed the rounding")
    assert len(ended) < 220  # a summary is cut, never dumped
    assert lines[-1] == f"cauce: #{n} attempt 2 passed"
    # every line is cauce's, and a reason that is no warning is not repeated
    assert all(line.startswith("cauce: ") for line in lines)
    assert not any("ladder" in line for line in lines)


def test_a_dry_run_and_a_run_without_a_listener_say_nothing(git_repo, store, capsys):
    run("check it in the browser", git_repo, store, Options(dry_run=True), registry={},
        classifier=kind("implement"), progress=lambda line: pytest.fail(line))
    report = run("fix the thing", git_repo, store, registry={}, launcher=Script(ok()), classifier=kind("implement"))
    assert report.status == "done"
    assert capsys.readouterr() == ("", "")
