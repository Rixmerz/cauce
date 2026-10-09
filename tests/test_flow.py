from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cauce import dispatch, flow
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


def test_a_task_a_person_cancelled_does_not_pause_the_queue(git_repo, store: Store):
    key = str(git_repo.resolve())
    for text in ("first", "second"):
        store.enqueue(text, repo=key, cwd=str(git_repo))

    def cancel_first(spec):
        store.request_cancel(store.list_tasks(status=["running"])[0]["id"], via="cli")
        return bad(Failure.CODE_BUG, "half done")

    results = [cancel_first, lambda spec: ok()]

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=results.pop(0),
                   classifier=kind("implement"), adapters=[], **kw)

    report = flow.work(store, runner=runner)
    assert [r.status for r in report.ran] == ["cancelled", "done"]
    assert not any(lane["paused"] for lane in store.lanes())


def test_the_unattended_limit_and_a_vanished_directory(git_repo, store: Store, tmp_path):
    key = str(git_repo.resolve())
    for i in range(3):
        store.enqueue(f"t{i}", repo=key, cwd=str(git_repo))
    report = flow.work(store, max_tasks=2, runner=_runner([ok(), ok()]))
    assert len(report.ran) == 2 and "unattended limit" in report.stopped_because

    gone = store.enqueue("x", repo="elsewhere", cwd=str(tmp_path / "deleted"))
    flow.work(store, repo="elsewhere", runner=_runner([]))
    assert store.get_task(gone["id"])["status"] == "blocked"
    stop = store.last_event(gone["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "missing_dir" and str(tmp_path / "deleted") in stop["reason"]
    assert any(lane["repo"] == "elsewhere" and lane["paused"] for lane in store.lanes())


def _never(task, ahead):
    raise AssertionError("nothing should be decided here")


def test_a_busy_lane_waits_and_a_claim_happens_once(store: Store):
    a = store.enqueue("a", repo="r", cwd="/x")
    b = store.enqueue("b", repo="r", cwd="/x")
    other = store.enqueue("c", repo="other", cwd="/x")
    ids = lambda **kw: [t["id"] for t in flow.runnable(store, parallel=False, decide=_never, **kw)]  # noqa: E731
    assert ids(repo=None) == [other["id"], a["id"]]
    assert store.claim(a["id"]) and not store.claim(a["id"])
    assert ids(repo="r") == []  # serial: a is running from the dispatcher
    assert ids(repo=None) == [other["id"]]
    store.update_task(a["id"], status="done")
    assert ids(repo="r") == [b["id"]]


def test_haiku_lets_independent_work_start_beside_running_work(store: Store):
    running = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(running["id"])
    same = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    docs = store.enqueue("fix a typo in the README", repo="r", cwd="/x")
    asked = []

    def decide(task, ahead):
        asked.append((task["id"], [t["id"] for t in ahead]))
        if task["id"] == same["id"]:
            return dispatch.Decision(False, "touches the importer #1 rewrites", 0.002)
        return dispatch.Decision(True, "only the README", 0.001)

    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=decide)] == [docs["id"]]
    # Each one was judged against everything running and queued ahead of it.
    assert asked == [(same["id"], [running["id"]]), (docs["id"], [running["id"], same["id"]])]
    waiting = store.get_task(same["id"])
    assert waiting["parallel"] == 0 and "importer" in waiting["parallel_reason"] and waiting["cost_usd"] == 0.002
    # Judged once: the next look asks nothing.
    store.claim(docs["id"])
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    # Once the lane is idle, the task that waited is first and starts.
    for t in (running, docs):
        store.update_task(t["id"], status="done")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [same["id"]]


def test_never_more_than_the_parallel_cap(store: Store, monkeypatch):
    monkeypatch.setattr(flow, "MAX_PARALLEL", 2)
    for i in range(4):
        store.enqueue(f"read file {i}", repo="r", cwd="/x")
    yes = lambda task, ahead: dispatch.Decision(True, "read only")  # noqa: E731
    assert len(flow.runnable(store, "r", parallel=True, decide=yes)) == 2


def test_the_dispatcher_runs_parallel_work_and_waits_for_it(git_repo, store: Store):
    """Handles that finish on the dispatcher's own clock: two start together,
    the third waits for a slot, and the report holds all three."""
    key = str(git_repo.resolve())
    for text in ("a", "b", "c"):
        store.enqueue(text, repo=key, cwd=str(git_repo))
    live, ticks = [], []

    class Slow:
        def __init__(self, task_id):
            self.task_id, self.left = task_id, 2

        def poll(self):
            self.left -= 1
            if self.left > 0:
                return None
            store.update_task(self.task_id, status="done")
            return "done"

    def start(task, where):
        live.append(task["id"])
        return Slow(task["id"])

    report = flow.work(store, parallel=True, start=start, pause=lambda: ticks.append(1),
                       decide=lambda task, ahead: dispatch.Decision(True, "independent"))
    assert sorted(r.task_id for r in report.ran) == sorted(live) and len(live) == 3
    assert {r.status for r in report.ran} == {"done"} and "nothing runnable" in report.stopped_because
    assert ticks  # it waited on work in flight instead of returning early


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
    assert "999999" in store.get_task(dead["id"])["result"]
    assert store.get_task(prompt["id"])["result"] is None  # a prompt keeps its own answer
    assert store.last_event(prompt["id"], "finished")["data"]["stop"]["cause"] == "died"
    assert flow.alive(None) is False and flow.alive(1) is True and flow.alive(999999) is False


def test_pinned_runs_never_teach_the_router(git_repo, store: Store):
    from cauce.matrix import Cell

    for _ in range(3):
        run("x", git_repo, store, Options(start=Cell("opus", "max")), registry={},
            launcher=lambda spec: WorkerResult(True, None, "ok", "1 passed"), classifier=kind("implement"),
            adapters=[])
    assert store.landings("implement") == []
    assert store.list_tasks()[0]["pinned"] == 1


def test_prune_deletes_only_the_branches_whose_work_landed(store, git_repo, monkeypatch, capsys):
    """Merged, squash-merged and artifact-only branches go, each kept on its task's
    events with its tip; work not landed, a task that may resume, and a branch
    checked out in a worktree stay."""
    from cauce import cli, flow, isolate

    from .conftest import git

    def branch(task_id, files, status="done"):
        task = store.create_task(f"task {task_id}", status="running", source="cauce", cwd=str(git_repo))
        store.update_task(task["id"], status=status)
        name = isolate.branch_for(task["id"])
        git(git_repo, "checkout", "-qb", name)
        for path, text in files.items():
            (git_repo / path).parent.mkdir(parents=True, exist_ok=True)
            (git_repo / path).write_text(text)
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-qm", name)
        git(git_repo, "checkout", "-q", "main")
        return task["id"], name

    merged = branch(1, {"a.txt": "a"})
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", merged[1])
    squashed = branch(2, {"b.txt": "b"})
    (git_repo / "b.txt").write_text("b")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "squash")
    artifacts = branch(3, {".playwright-mcp/console.log": "x"})
    pending = branch(4, {"c.txt": "c"})
    blocked = branch(5, {"e.txt": "e"}, status="blocked")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", blocked[1])
    checked_out = branch(6, {"d.txt": "d"})
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", checked_out[1])
    git(git_repo, "worktree", "add", "-q", str(git_repo.parent / "wt"), checked_out[1])

    assert {x["task"] for x in flow.prune(store, dry_run=True)} == {merged[0], squashed[0], artifacts[0]}
    assert len(isolate.kept_branches(git_repo)) == 6  # a dry run deletes nothing

    monkeypatch.setenv("CAUCE_PRUNE", "off")
    assert flow.prune(store) == []
    assert cli.main(["prune"]) == 0
    assert "pruning is off" in capsys.readouterr().out

    monkeypatch.setenv("CAUCE_PRUNE", "on")
    gone = {x["task"]: x["why"] for x in flow.prune(store, [git_repo])}
    assert gone == {merged[0]: "merged", squashed[0]: "squash-merged", artifacts[0]: "artifacts only"}
    assert set(isolate.kept_branches(git_repo)) == {pending[0], blocked[0], checked_out[0]}
    event = store.last_event(merged[0], "branch_pruned")["data"]
    assert event["branch"] == merged[1]
    assert git(git_repo, "cat-file", "-t", event["tip"]) == "commit"  # still there to bring back
    assert cli.main(["prune", "--repo", str(git_repo)]) == 0
    assert "no branch cauce kept has landed" in capsys.readouterr().out
