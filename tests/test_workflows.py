from __future__ import annotations

import json

import pytest

from cauce import cli, flow, project, workflows
from cauce.escalate import Failure
from cauce.orchestrate import run
from cauce.store import Store

from .test_orchestrate import bad, kind, ok


@pytest.fixture
def kicks(monkeypatch):
    """No real dispatcher: each start the engine asks for is recorded."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: seen.append((cwd, key)))
    return seen


def _worker(results, seen):
    """A launcher that writes one file per writing attempt and records what each attempt saw."""
    results = list(results)

    def launch(spec):
        files = sorted(p.name for p in spec.target_dir.iterdir() if p.suffix == ".txt")
        seen.append({"prompt": spec.prompt, "files": files, "dir": spec.target_dir,
                     "reads_only": "Edit" in spec.disallowed_tools})
        if "Edit" not in spec.disallowed_tools:
            (spec.target_dir / f"step{len(seen)}.txt").write_text("work\n")
        return results.pop(0)

    return launch


def _runner(results, seen):
    launch = _worker(results, seen)

    def runner(text, where, store, options, **kw):
        return run(text, where, store, options, registry={}, launcher=launch, classifier=kind("implement"),
                   adapters=[], **kw)

    return runner


def _save(defn, cwd, scope="user"):
    return workflows.save(defn, cwd, scope)


CHAIN = {"name": "chain", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "check", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine"},
    {"id": "polish", "prompt": "Polish {goal}", "kind": "implement"},
]}


# --- definitions ---------------------------------------------------------------------


def test_a_definition_is_checked_and_completed():
    defn = workflows.normalize(CHAIN)
    assert [s["needs"] for s in defn["steps"]] == [[], ["build"], ["check"]]
    assert defn["steps"][0]["title"] == "build"
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize({"name": "x", "inputs": ["goal"], "steps": [
            {"id": "a", "prompt": "do {other}", "kind": "nope", "allow_tools": ["Bash(*)"]},
            {"id": "a", "prompt": "", "needs": ["z"], "start": "gpt/high", "budget_usd": -1}]})
    said = str(err.value)
    for part in ("allow_tools", "flags of `cauce workflow run`", "{other}", "no kind 'nope'", "used twice",
                 "needs a prompt", "'z'", "no cell", "budget_usd"):
        assert part in said
    with pytest.raises(workflows.WorkflowError, match="no workflow name"):
        workflows.skeleton("Bad Name")


def test_make_copy_change_and_remove_workflows(git_repo):
    project.enroll(git_repo)
    names = {(w["name"], w["scope"]) for w in workflows.listing(git_repo)}
    assert {("feature", "bundled"), ("bugfix", "bundled"), ("review-fix", "bundled")} <= names

    # A copy of a template, in the project, hides the template there.
    workflows.copy_to("feature", "feature", git_repo, scope="project")
    found = [w for w in workflows.listing(git_repo) if w["name"] == "feature"]
    assert [(w["scope"], w["shadowed"]) for w in found] == [("project", False), ("bundled", True)]
    assert workflows.load("feature", git_repo)["scope"] == "project"

    # A change to a bundled template lands in the user scope; the template stays.
    where, path = workflows.edit("bugfix", git_repo, lambda d: d.update(description="mine"))
    assert where == "user" and json.loads(path.read_text())["description"] == "mine"
    assert workflows.load("bugfix", git_repo, "bundled")["description"] != "mine"

    _save(workflows.skeleton("mine"), git_repo)
    with pytest.raises(workflows.WorkflowError, match="exists"):
        _save(workflows.skeleton("mine"), git_repo)

    def grow(d):
        workflows.add_step(d, {"id": "test", "prompt": "Test {goal}"})
        workflows.add_step(d, {"id": "plan", "prompt": "Plan {goal}"}, after=None)
        workflows.add_step(d, {"id": "first", "prompt": "First {goal}"}, after="do")
        workflows.set_step(d, "test", {"kind": "test", "verify": "pytest -q"})

    workflows.edit("mine", git_repo, grow)
    steps = workflows.load("mine", git_repo)["steps"]
    assert [(s["id"], s["needs"]) for s in steps] == [
        ("do", []), ("first", ["do"]), ("test", ["first"]), ("plan", ["test"])]
    workflows.edit("mine", git_repo, lambda d: (workflows.remove_step(d, "first"),
                                                workflows.set_step(d, "test", {"verify": None})))
    steps = workflows.load("mine", git_repo)["steps"]
    assert [(s["id"], s["needs"]) for s in steps] == [("do", []), ("test", ["do"]), ("plan", ["test"])]
    assert "verify" not in steps[1]
    with pytest.raises(workflows.WorkflowError, match="no step"):
        workflows.edit("mine", git_repo, lambda d: workflows.set_step(d, "ghost", {"kind": "docs"}))

    with pytest.raises(workflows.WorkflowError, match="bundled template is not removed"):
        workflows.remove("feature", git_repo, "bundled")
    workflows.remove("mine", git_repo, "user")
    with pytest.raises(workflows.WorkflowError, match="no workflow 'mine'"):
        workflows.load("mine", git_repo)
    with pytest.raises(workflows.WorkflowError, match="no enrolled project"):
        workflows.save(workflows.skeleton("x"), git_repo.parent, "project")


# --- runs ----------------------------------------------------------------------------


def test_a_run_goes_on_by_itself_each_step_on_the_work_before_it(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"}, session_id="s1",
                              options={"budget_usd": 2.0})
    assert len(started["queued"]) == 1 and kicks
    seen: list[dict] = []
    report = flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], seen))
    assert [r.status for r in report.ran] == ["done", "done", "done"]

    build, check, polish = seen
    assert "Task (implement):\nBuild the export" in build["prompt"]
    # The reading step reads the build in a worktree of its own, and cannot write.
    assert check["reads_only"] and "step1.txt" in check["files"]
    assert check["dir"] != git_repo and "git diff" in check["prompt"]
    # The last step starts from the build, and hears what the steps before found.
    assert "step1.txt" in polish["files"]
    assert "- build (task #" in polish["prompt"] and "no problems" in polish["prompt"]

    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["branch"] == f"cauce/task-{status['steps'][2]['task']}"
    assert [s["state"] for s in status["steps"]] == ["done", "done", "done"]
    tasks = store.run_tasks(started["id"])
    assert all(t["session_id"] == "s1" for t in tasks)
    assert json.loads(tasks[0]["options"])["budget_usd"] == 2.0
    # The session hears of the run once, at its end: not of each step it went past.
    assert [t["id"] for t in store.unreported("s1")] == [tasks[-1]["id"]]
    # A watcher sees the run, and each step's task says which run it belongs to.
    from cauce.ui import api

    seen_now = api.overview(store, hours=1)
    assert [w["id"] for w in seen_now["workflows"]] == [started["id"]]
    assert any(t.get("workflow_step") == "polish" for s in seen_now["sessions"] for t in s["tasks"]) or \
        any(t.get("workflow_step") == "polish" for t in seen_now["other"])


def test_a_step_that_stops_waits_for_the_person_then_the_run_goes_on(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be read")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "waits on you" and status["waiting"] == ["check"]
    assert [s["state"] for s in status["steps"]] == ["done", "replan", "pending"]
    # The session hears of the stop.
    assert status["steps"][1]["task"] in [t["id"] for t in store.unreported("s1")]

    with pytest.raises(workflows.WorkflowError, match="waits on"):
        workflows.retry(store, started["id"], "polish")
    again = workflows.retry(store, started["id"], "check")
    assert again["workflow_step"] == "check"
    with pytest.raises(workflows.WorkflowError, match="nothing to retry"):
        workflows.retry(store, started["id"], "check")
    assert not any(lane["paused"] for lane in store.lanes())  # the retry opened the lane its stop paused
    flow.work(store, runner=_runner([ok(), ok()], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][1]["tries"] == 2


def test_two_steps_a_third_waits_on_queue_it_once(git_repo, store: Store, kicks):
    _save({"name": "fan", "steps": [
        {"id": "a", "prompt": "Write part a", "kind": "implement"},
        {"id": "b", "prompt": "Write part b", "kind": "implement", "needs": ["a"]},
        {"id": "c", "prompt": "Write part c", "kind": "implement", "needs": ["a"]},
        {"id": "d", "prompt": "Join them", "kind": "implement", "needs": ["b", "c"]}]}, git_repo)
    started = workflows.start(store, "fan", git_repo)
    flow.work(store, parallel=False, runner=_runner([ok()] * 4, []))
    tasks = store.run_tasks(started["id"])
    assert [t["workflow_step"] for t in tasks] == ["a", "b", "c", "d"]
    assert workflows.status(store, started["id"])["state"] == "done"
    # Advancing again finds nothing more to queue.
    assert workflows.advance(store, tasks[1]) == []


def test_a_cancelled_run_starts_nothing_more(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    started = workflows.start(store, "chain", git_repo, {"goal": "x"})
    stopped = workflows.cancel(store, started["id"])
    assert stopped == [store.run_tasks(started["id"])[0]["id"]]
    status = workflows.status(store, started["id"])
    assert status["state"] == "cancelled" and status["steps"][0]["state"] == "cancelled"
    with pytest.raises(workflows.WorkflowError, match="cancelled"):
        workflows.retry(store, started["id"], "build")
    assert workflows.advance(store, {**store.run_tasks(started["id"])[0], "status": "done"}) == []


def test_a_run_needs_its_inputs_and_only_those(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    with pytest.raises(workflows.WorkflowError, match="needs goal"):
        workflows.start(store, "chain", git_repo, {})
    with pytest.raises(workflows.WorkflowError, match="takes no input colour"):
        workflows.start(store, "chain", git_repo, {"goal": "x", "colour": "red"})
    assert store.list_runs() == []


def test_the_cli_makes_changes_runs_and_shows(git_repo, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "new", "ship", "--description", "build and test"]) == 0
    assert cli.main(["workflow", "step", "set", "ship", "do", "--prompt", "Build {goal}", "--kind", "implement"]) == 0
    assert cli.main(["workflow", "step", "add", "ship", "test", "--prompt", "Test {goal}", "--kind", "test",
                     "--verify", "pytest -q"]) == 0
    assert cli.main(["workflow", "step", "set", "ship", "test", "--clear", "verify"]) == 0
    assert cli.main(["workflow", "copy", "ship", "ship2"]) == 0
    assert cli.main(["workflow", "step", "rm", "ship2", "test"]) == 0
    assert cli.main(["workflow", "validate", "ship"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "ship", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in shown["steps"]] == ["do", "test"] and "verify" not in shown["steps"][1]
    assert cli.main(["workflow", "list"]) == 0
    assert "ship2" in capsys.readouterr().out
    assert cli.main(["workflow", "step", "add", "ship", "x", "--prompt", "{nope}"]) == 1
    assert "not among the inputs" in capsys.readouterr().err

    assert cli.main(["workflow", "run", "ship", "the export", "--budget", "3", "--json"]) == 0
    run_status = json.loads(capsys.readouterr().out)
    assert run_status["state"] == "running" and run_status["now"] == ["do"]
    assert cli.main(["workflow", "status"]) == 0
    out = capsys.readouterr().out
    assert f"run #{run_status['id']} ship — running" in out and "now: do" in out
    assert cli.main(["workflow", "retry", str(run_status["id"]), "do"]) == 1
    assert cli.main(["workflow", "cancel", str(run_status["id"])]) == 0
    assert cli.main(["workflow", "rm", "ship2", "--scope", "user"]) == 0
    assert cli.main(["workflow", "run", "feature"]) == 1
    assert "needs goal" in capsys.readouterr().err


def test_the_cli_reads_writes_files_and_says_where_a_stopped_run_waits(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch, tmp_path):
    monkeypatch.chdir(git_repo)
    project.enroll(git_repo)
    draft = tmp_path / "w.json"
    draft.write_text(json.dumps(CHAIN))
    assert cli.main(["workflow", "save", str(draft), "--scope", "project", "--name", "chained"]) == 0
    assert cli.main(["workflow", "save", str(draft), "--scope", "project", "--name", "chained"]) == 1
    assert cli.main(["workflow", "validate", "--file", str(draft)]) == 0
    assert cli.main(["workflow", "list", "--json"]) == 0
    assert "chained" in {w["name"] for w in json.loads(capsys.readouterr().out.splitlines()[-1])}
    assert cli.main(["workflow", "show", "chained"]) == 0
    out = capsys.readouterr().out
    assert "check — check after build [kind review-routine]" in out and "inputs: goal" in out
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    assert cli.main(["workflow", "edit", "chained"]) == 1
    assert "no editor here" in capsys.readouterr().err
    assert cli.main(["workflow", "new", "fresh", "--from", "bugfix", "--scope", "project"]) == 0
    assert cli.main(["workflow", "run", "chained", "--input", "goal=the export", "--input", "oops"]) == 1
    assert cli.main(["workflow", "run", "chained", "--input", "goal=the export"]) == 0
    capsys.readouterr()
    run_id = store.list_runs()[0]["id"]
    flow.work(store, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot")], []))
    assert cli.main(["workflow", "status", str(run_id)]) == 0
    out = capsys.readouterr().out
    assert "waits on you" in out and "✓ build" in out and f"retry {run_id} <step>" in out
    assert "work so far: branch cauce/task-" in out
    assert cli.main(["workflow", "status", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["state"] == "waits on you"
    assert cli.main(["overview"]) == 0
    out = capsys.readouterr().out
    assert f"run #{run_id} chained — waits on you" in out and "(workflow run #" in out
    assert cli.main(["workflow", "retry", str(run_id), "check"]) == 0
    assert cli.main(["workflow", "status", "999"]) == 1


def test_a_step_carries_the_memory_attached_to_it(git_repo, store: Store, kicks):
    from cauce import repo

    key = repo.key(git_repo.resolve())
    zone = store.add_note(key, "conventions", "Zones", "Zone ids are UTC offsets, never city names.",
                          author="person", filed_by="person")
    old = store.add_note(key, "decisions", "Old rule", "Use floats for money.", author="person", filed_by="person")
    new = store.add_note(key, "decisions", "Money", "Money is integer cents.", author="person", filed_by="person")
    store.update_note(old, state="replaced", replaced_by=new)
    store.add_note(key, "zones", "Daylight", "Daylight saving shifts zone offsets twice a year.",
                   author="person", filed_by="person")
    elsewhere = store.add_note("other/repo", "zones", "Not ours", "Never shown here.", author="person",
                               filed_by="person")
    problem = store.open_problem("Totals off by one cent", repo=key, symptom="the cart total rounds twice")
    store.add_fix(problem, "round each line", outcome="failed", repo=key, why="the error adds up")

    _save({"name": "zoned", "inputs": ["goal"], "steps": [
        {"id": "build", "prompt": "Build {goal}", "kind": "implement",
         "memory": [zone, f"note:{old}", "topic:zones", f"problem:{problem}"]},
        {"id": "doc", "prompt": "Document {goal}", "kind": "docs"}]}, git_repo)
    assert workflows.load("zoned", git_repo)["steps"][0]["memory"] == [
        f"note:{zone}", f"note:{old}", "topic:zones", f"problem:{problem}"]
    started = workflows.start(store, "zoned", git_repo, {"goal": "the scheduler"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok(), ok()], seen))
    build, doc = seen[0]["prompt"], seen[1]["prompt"]
    assert "Memory this workflow attaches to this step" in build
    assert "Zone ids are UTC offsets" in build and "Daylight saving shifts" in build
    assert "Money is integer cents." in build and "Use floats" not in build  # the note that replaced it
    assert "Totals off by one cent" in build and "failed: round each line — the error adds up" in build
    assert "Never shown here" not in build and "Memory this workflow" not in doc
    assert workflows.status(store, started["id"])["state"] == "done"

    # What cannot be read here stops the run before it starts.
    store.update_note(zone, state="dropped")
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.start(store, "zoned", git_repo, {"goal": "x"})
    assert f"note #{zone} was dropped" in str(err.value)
    workflows.edit("zoned", git_repo, lambda d: workflows.set_step(d, "build", {"memory": [
        f"note:{elsewhere}", "topic:nothing", "problem:999"]}))
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.start(store, "zoned", git_repo, {"goal": "x"})
    for part in (f"no note #{elsewhere} in this project", "topic 'nothing' holds no live note", "no problem #999"):
        assert part in str(err.value)
    with pytest.raises(workflows.WorkflowError, match="memory is a list"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "do", "memory": ["zone:1"]}]})


def test_the_cli_attaches_and_clears_memory(git_repo, store: Store, kicks, capsys, monkeypatch):
    from cauce import repo

    monkeypatch.chdir(git_repo)
    note = store.add_note(repo.key(git_repo.resolve()), "zones", "Zones", "UTC offsets.", author="person",
                          filed_by="person")
    assert cli.main(["workflow", "new", "z"]) == 0
    assert cli.main(["workflow", "step", "set", "z", "do", "--memory", f"{note},topic:zones"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "z"]) == 0
    assert f"+ memory: note:{note}, topic:zones" in capsys.readouterr().out
    assert cli.main(["workflow", "step", "set", "z", "do", "--memory", "zone:3"]) == 1
    assert cli.main(["workflow", "step", "set", "z", "do", "--clear", "memory"]) == 0
    assert "memory" not in workflows.load("z", git_repo)["steps"][0]
