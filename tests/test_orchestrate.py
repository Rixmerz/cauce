from __future__ import annotations

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
    assert report.moves[0].startswith("more_effort")
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
    script = Script(bad(Failure.CODE_BUG), bad(Failure.CODE_BUG))
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
