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


def test_a_held_back_task_says_what_it_waits_for_wherever_it_is_shown(capsys, store: Store):
    from cauce import cli
    from cauce.ui import api

    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(first["id"])
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")

    def decide(task, ahead):
        if task["id"] == held["id"]:
            return dispatch.Decision(False, f"it changes the importer #{first['id']} rewrites")
        return dispatch.Decision(False, "the model answered outside the schema")

    assert flow.runnable(store, "r", parallel=True, decide=decide) == []
    # The decision is an event when it is made, with the task it waits for: the one
    # its reason names, else the first ahead of it.
    decided = store.last_event(held["id"], "dispatch_decided")["data"]
    assert decided == {"parallel": False, "reason": f"it changes the importer #{first['id']} rewrites",
                       "behind": first["id"], "ahead": [first["id"], second["id"]], "by": "haiku"}
    assert store.last_event(second["id"], "dispatch_decided")["data"]["behind"] == first["id"]
    line = f"waits for #{first['id']}: it changes the importer #{first['id']} rewrites"
    assert dispatch.waiting(store, store.get_task(held["id"])) == line

    assert cli.main(["queue", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert line in out and out.index("add a flag") < out.index(line)
    assert cli.main(["tasks", "--all"]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["show", str(held["id"])]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["show", str(first["id"])]) == 0
    assert "waits" not in capsys.readouterr().out  # a running task waits for nothing

    lane = next(q for q in api.board(store)["queued"] if q["repo"] == "r")
    assert {t["id"]: t["waits"] for t in lane["tasks"]}[held["id"]] == line
    assert api.task_detail(store, held["id"])["task"]["waits"] == line

    # A decision kept before the event existed names the first task now ahead of it.
    old = store.enqueue("older", repo="r", cwd="/x")
    store.set_parallel(old["id"], False, "may conflict")
    assert dispatch.waiting(store, store.get_task(old["id"])) == f"waits for #{first['id']}: may conflict"
    store.set_parallel(old["id"], True, "independent")
    assert dispatch.waiting(store, store.get_task(old["id"])) is None


def test_what_a_held_back_task_waits_for_moves_on_with_its_lane(capsys, store: Store):
    from cauce import cli

    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    store.claim(first["id"])
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    assert flow.runnable(store, "r", parallel=True,
                         decide=lambda task, ahead: dispatch.Decision(False, "both change the importer")) == []
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{first['id']}: both change the importer"

    # The task it was held behind ends and the next one starts: the decision stands, made
    # once, and the line names the task it is behind now, and says whom the reason was about.
    store.update_task(first["id"], status="done")
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [second["id"]]
    store.claim(second["id"])
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    line = f"waits for #{second['id']}: both change the importer (judged against #{first['id']})"
    assert dispatch.waiting(store, store.get_task(held["id"])) == line
    assert cli.main(["queue", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert line in out and f"waits for #{first['id']}" not in out

    # Every task it was judged against has gone: the one ahead now, and the reason says whom it was about.
    store.update_task(second["id"], status="done")
    later = store.enqueue("a typo in the readme", repo="r", cwd="/x")
    store.claim(later["id"])
    assert dispatch.waiting(store, store.get_task(held["id"])) == (
        f"waits for #{later['id']}: both change the importer (judged against #{first['id']})")
    assert cli.main(["show", str(held["id"])]) == 0
    assert f"waits for #{later['id']}: " in capsys.readouterr().out

    # Nothing ahead of it any more: it waits for no task, and starts next.
    store.update_task(later["id"], status="done")
    assert dispatch.waiting(store, store.get_task(held["id"])) is None
    assert [t["id"] for t in flow.runnable(store, "r", parallel=True, decide=_never)] == [held["id"]]


def test_a_reason_about_a_task_that_ended_says_it_when_another_one_is_still_ahead(store: Store):
    first = store.enqueue("rewrite the importer", repo="r", cwd="/x")
    second = store.enqueue("refactor the parser", repo="r", cwd="/x")
    store.claim(first["id"])
    store.claim(second["id"])
    held = store.enqueue("add a flag to the importer", repo="r", cwd="/x")
    reason = f"it changes the importer #{first['id']} rewrites"
    assert flow.runnable(store, "r", parallel=True, decide=lambda task, ahead: dispatch.Decision(False, reason)) == []
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{first['id']}: {reason}"

    # #1 ended and #2, which it was judged against too, still runs: the line names #2,
    # and the reason about #1 does not read as one about #2.
    store.update_task(first["id"], status="done")
    assert dispatch.waiting(store, store.get_task(held["id"])) == (
        f"waits for #{second['id']}: {reason} (judged against #{first['id']})")


def test_a_model_that_did_not_answer_reads_as_waiting_once(store: Store):
    running = store.enqueue("a", repo="r", cwd="/x")
    store.claim(running["id"])
    held = store.enqueue("b", repo="r", cwd="/x")
    flow.runnable(store, "r", parallel=True,
                  decide=lambda task, ahead: dispatch.Decision(False, "waits its turn: claude not found"))
    assert dispatch.waiting(store, store.get_task(held["id"])) == f"waits for #{running['id']}: claude not found"


def test_surfaces_that_clash_name_the_task_they_wait_for(store: Store):
    running = store.enqueue("a", repo="r", cwd="/x", options={"surfaces": ["src/**"]})
    store.claim(running["id"])
    clash = store.enqueue("b", repo="r", cwd="/x", options={"surfaces": ["src/api/**"]})
    assert flow.runnable(store, "r", parallel=True, decide=_never) == []
    decided = store.last_event(clash["id"], "dispatch_decided")["data"]
    assert decided["by"] == "surfaces" and decided["behind"] == running["id"] and not decided["parallel"]
    assert dispatch.waiting(store, store.get_task(clash["id"])).startswith(f"waits for #{running['id']}: ")


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
    # A budget an older cauce stored is left where it is: nothing reads it.
    task = {"options": '{"budget_usd": "x", "verify": "pytest", "kind": "docs", "start": "sonnet/high"}'}
    options = flow.queued_options(task, Options(max_attempts=3))
    assert (options.verify, options.kind, options.start.label, options.max_attempts) == (
        "pytest", "docs", "sonnet/high", 3)
    assert not hasattr(options, "budget_usd")
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
    assert store.last_event(prompt["id"], "finished")["data"]["stop"]["cause"] == "turn"  # a turn, not work to resume
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


def test_an_investigator_that_stops_leaves_the_person_s_lane_open(git_repo, store: Store):
    key = str(git_repo.resolve())
    store.enqueue("investigate", repo=key, cwd=str(git_repo), options={"investigation": "inv-1", "kind": "explore"})
    store.enqueue("the person's own task", repo=key, cwd=str(git_repo))
    report = flow.work(store, runner=_runner([bad(Failure.SPEC_BUG, "cannot read it"), ok()]))
    assert [r.status for r in report.ran] == ["replan", "done"]
    assert not any(lane["paused"] for lane in store.lanes())


def test_prune_deletes_a_finished_copy_of_work_another_branch_keeps(store, git_repo):
    from cauce import flow, isolate

    from .conftest import git

    ids = []
    for _ in range(3):
        task = store.create_task("rename the field", status="done", source="cauce", cwd=str(git_repo))
        ids.append(task["id"])
        git(git_repo, "checkout", "-qb", isolate.branch_for(task["id"]))
        (git_repo / "field.py").write_text("renamed\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-qm", f"try {task['id']}")
        git(git_repo, "checkout", "-q", "main")
    store.update_task(ids[2], status="blocked")  # the newest may resume: it stays
    pruned = flow.prune(store, [git_repo])
    assert [(p["task"], p["why"]) for p in pruned] == [(ids[0], f"the same work as cauce/task-{ids[1]}"),
                                                        (ids[1], f"the same work as cauce/task-{ids[2]}")]
    assert list(isolate.kept_branches(git_repo)) == [ids[2]]


def test_a_task_asked_again_points_at_the_one_that_keeps_its_work(git_repo, store):
    from cauce.escalate import Failure as F

    from .test_orchestrate import Script

    first = run("migrate the totals to cents", git_repo, store, registry={}, adapters=[],
                launcher=Script(WorkerResult(False, F.PERMISSION, "refused", denied=("Bash(x)",)), write="m.py"),
                classifier=kind("implement"))
    said = []
    run("migrate the totals to cents", git_repo, store, registry={}, adapters=[], launcher=Script(ok()),
        classifier=kind("implement"), progress=said.append)
    assert any(f"task #{first.task_id} asked the same and keeps its work" in s and f"cauce resume {first.task_id}" in s
               for s in said)
