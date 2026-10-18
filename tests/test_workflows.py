from __future__ import annotations

import json

import pytest

from cauce import cli, flow, project, workflows
from cauce.escalate import Failure
from cauce.launch import WorkerResult
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
    return workflows.save(defn, cwd, scope).path


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
            {"id": "a", "prompt": "", "needs": ["z"], "start": "gpt/high", "max_turns": 0}]})
    said = str(err.value)
    for part in ("allow_tools", "flags of `cauce workflow run`", "{other}", "no kind 'nope'", "used twice",
                 "needs a prompt", "'z'", "no cell", "max_turns is a whole number from 1"):
        assert part in said
    # A step an older cauce saved with a dollar budget still loads; the key is dropped, even a bad value.
    # Turns over the ceiling (an older cauce took any number) read as the ceiling.
    old = workflows.normalize({"name": "old", "inputs": ["goal"], "steps": [
        {"id": "a", "prompt": "do {goal}", "kind": "implement", "budget_usd": 2.0, "max_turns": 300},
        {"id": "b", "prompt": "check", "kind": "review-routine", "needs": ["a"], "budget_usd": -1}]})
    assert all("budget_usd" not in step for step in old["steps"])
    assert old["steps"][0]["max_turns"] == 200
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
    where, path, _ = workflows.edit("bugfix", git_repo, lambda d: d.update(description="mine"))
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
                              options={"allow_tools": ["Bash(make:*)"]})
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
    assert "budget_usd" not in json.loads(tasks[0]["options"])
    assert json.loads(tasks[0]["options"])["allow_tools"] == ["Bash(make:*)"]
    # The session hears of the run once, at its end: not of each step it went past.
    assert [t["id"] for t in store.unreported("s1")] == [tasks[-1]["id"]]
    # A watcher sees the run, and each step's task says which run it belongs to.
    from cauce.ui import api

    seen_now = api.overview(store, hours=1)
    assert [w["id"] for w in seen_now["workflows"]] == [started["id"]]
    assert any(t.get("workflow_step") == "polish" for s in seen_now["sessions"] for t in s["tasks"]) or \
        any(t.get("workflow_step") == "polish" for t in seen_now["other"])


def test_a_run_says_its_first_steps_warnings_and_an_ending_repeats_them(git_repo, store: Store, kicks, capsys,
                                                                        monkeypatch):
    from cauce import hooks

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("CAUCE_SESSION_ID", "s1")
    _save({"name": "look", "inputs": ["page"], "steps": [
        {"id": "edit", "prompt": "Change {page}", "kind": "implement"},
        {"id": "see", "prompt": "Open {page} in the browser and fix what is off", "kind": "implement",
         "needs": ["edit"]}]}, git_repo)
    _save({"name": "shot", "steps": [
        {"id": "snap", "prompt": "Take a screenshot of the home page and fix it", "kind": "implement"}]}, git_repo)
    assert cli.main(["workflow", "run", "shot"]) == 0
    out = capsys.readouterr().out
    snap = store.run_tasks(store.list_runs()[0]["id"])[0]["id"]
    assert f"warning: step snap (#{snap}): this task needs a browser and workers have none" in out
    assert cli.main(["workflow", "run", "shot", "--json"]) == 0
    assert "needs a browser" in json.loads(capsys.readouterr().out)["warnings"][0]["warning"]
    assert cli.main(["workflow", "cancel", "1"]) == 0
    assert cli.main(["workflow", "cancel", "2"]) == 0
    # A step that waits on another is not planned yet: nothing to say of it.
    assert cli.main(["workflow", "run", "look", "the settings page"]) == 0
    assert "warning:" not in capsys.readouterr().out
    run_id = store.list_runs()[0]["id"]
    flow.work(store, runner=_runner([ok(), ok()], []))
    edit, see = store.run_tasks(run_id)
    assert store.last_event(edit["id"], "planned")["data"]["warnings"] == []
    assert "needs a browser" in store.last_event(see["id"], "planned")["data"]["warnings"][0]
    # The run's ending is its last step's, and it repeats the warning that step ran with.
    text, ids = hooks.endings(store, "s1")
    assert see["id"] in ids and edit["id"] not in ids
    ending = text[text.index(f"#{see['id']} [done]"):]
    assert ending.splitlines()[1].startswith("  it ran with a warning: this task needs a browser and workers have none")


def test_a_plan_that_cannot_be_foreseen_never_undoes_the_start_of_a_run(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch):
    from cauce import orchestrate

    def broken(*args, **kwargs):
        raise ValueError("the registry is unreadable")

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(orchestrate, "foresee", broken)
    _save({"name": "shot", "steps": [
        {"id": "snap", "prompt": "Take a screenshot of the home page and fix it", "kind": "implement"}]}, git_repo)
    assert cli.main(["workflow", "run", "shot"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("started run #1 of shot")
    assert "note: the warnings of the first steps could not be foreseen (ValueError: the registry is unreadable)" in out
    assert "warning:" not in out
    assert [r["id"] for r in store.list_runs()] == [1]
    assert cli.main(["workflow", "run", "shot", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["warnings"] == [] and shown["warnings_error"] == "ValueError: the registry is unreadable"


def test_the_warning_of_a_step_the_engine_went_past_is_said_by_the_run_s_next_ending(git_repo, store: Store,
                                                                                      kicks):
    from cauce import hooks

    _save({"name": "look", "steps": [
        {"id": "see", "prompt": "Open the settings page in the browser and fix what is off", "kind": "implement"},
        {"id": "check", "prompt": "Make the settings form validate its fields", "kind": "implement"},
        {"id": "polish", "prompt": "Polish the settings module", "kind": "implement", "needs": ["check"]}]},
        git_repo)
    started = workflows.start(store, "look", git_repo, session_id="s1")
    # see and check start together; see passes, and the engine goes past it while check runs.
    flow.work(store, parallel=False, runner=_runner([ok(), bad(Failure.SPEC_BUG, "cannot be read")], []))
    see, check = store.run_tasks(started["id"])
    assert see["status"] == "done" and check["status"] == "replan"
    text, ids = hooks.endings(store, "s1", claim="stop")
    assert ids == [check["id"]]
    ending = text[text.index(f"#{check['id']} [replan]"):].splitlines()
    assert ending[1].startswith(f"  step see (#{see['id']}) ran with a warning: this task needs a browser "
                                "and workers have none")

    # Said once: the run's later endings do not repeat what the session already heard.
    workflows.retry(store, started["id"], "check")
    flow.work(store, runner=_runner([ok(), ok()], []))
    assert workflows.status(store, started["id"])["state"] == "done"
    text, ids = hooks.endings(store, "s1", claim="stop")
    polish = store.run_tasks(started["id"])[-1]
    assert ids == [polish["id"]] and "ran with a warning" not in text


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

    assert cli.main(["workflow", "run", "ship", "the export", "--json"]) == 0
    run_status = json.loads(capsys.readouterr().out)
    assert run_status["state"] == "running" and run_status["now"] == ["do"]
    assert cli.main(["workflow", "status"]) == 0
    out = capsys.readouterr().out
    assert f"#{run_status['id']} ship — running" in out and "step do (1/2): queued" in out
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
    for part in (f"no note #{elsewhere} in this project", "no problem #999"):
        assert part in str(err.value)
    # A topic with no note yet (a new project has none) is no reason not to start: it is said.
    workflows.edit("zoned", git_repo, lambda d: workflows.set_step(d, "build", {"memory": ["topic:nothing"]}))
    started = workflows.start(store, "zoned", git_repo, {"goal": "x"})
    assert started["gaps"] and "topic 'nothing' holds no note in this project yet" in started["gaps"][0]
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


def test_a_global_workflow_in_use_is_held_and_its_changes_wait(git_repo, tmp_path, store: Store, kicks, capsys,
                                                                monkeypatch):
    """One workflow in the user scope, used by two projects at once: no change reaches it under them."""
    other = tmp_path / "other"
    other.mkdir()
    import subprocess

    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)
    path = _save(CHAIN, git_repo)
    assert json.loads(path.read_text())["version"] == 1
    first = workflows.start(store, "chain", git_repo, {"goal": "a"})
    second = workflows.start(store, "chain", other, {"goal": "b"})
    assert workflows.in_use(path) == [first["id"], second["id"]]

    saved = workflows.edit("chain", git_repo, lambda d: workflows.set_step(d, "polish", {"title": "Shine"}))
    assert saved.held_by == [first["id"], second["id"]] and saved.path == workflows.pending_path(path)
    assert json.loads(path.read_text())["version"] == 1  # the definition itself did not move
    # A second change builds on the waiting one.
    workflows.edit("chain", git_repo, lambda d: workflows.set_step(d, "check", {"title": "Look"}))
    waiting = json.loads(workflows.pending_path(path).read_text())
    assert waiting["version"] == 2 and {s["id"]: s.get("title") for s in waiting["steps"]} == {
        "build": None, "check": "Look", "polish": "Shine"}
    with pytest.raises(workflows.WorkflowError, match="in use by run"):
        workflows.remove("chain", git_repo, "user")
    with pytest.raises(workflows.WorkflowError, match="in use by run"):
        workflows.apply_pending("chain", git_repo)
    assert [w["held_by"] for w in workflows.pending(git_repo)] == [[first["id"], second["id"]]]

    # A run keeps the copy it started with; the change waits for the last run to end.
    workflows.cancel(store, second["id"])
    assert workflows.pending_path(path).is_file()
    flow.work(store, runner=_runner([ok(), ok(), ok()], []))
    assert workflows.status(store, first["id"])["state"] == "done"
    assert workflows.status(store, first["id"])["steps"][2]["title"] == "polish"
    assert not workflows.pending_path(path).exists()
    live = json.loads(path.read_text())
    assert live["version"] == 2 and live["steps"][2]["title"] == "Shine"
    assert workflows.in_use(path) == []

    # --now writes at once, and a run started now uses the new version.
    third = workflows.start(store, "chain", git_repo, {"goal": "c"})
    assert workflows.edit("chain", git_repo, lambda d: d.update(description="now"), now=True).held_by == []
    assert json.loads(path.read_text())["version"] == 3
    assert workflows.status(store, third["id"])["version"] == 2

    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "step", "set", "chain", "build", "--title", "Make"]) == 0
    assert "in use by run" in capsys.readouterr().out
    assert cli.main(["workflow", "show", "chain"]) == 0
    out = capsys.readouterr().out
    assert "chain v3" in out and f"in use by run #{third['id']}" in out and "a pending change waits" in out
    assert cli.main(["workflow", "pending"]) == 0
    assert f"waits for run #{third['id']}" in capsys.readouterr().out
    assert cli.main(["workflow", "pending", "chain", "--apply"]) == 1
    assert cli.main(["workflow", "pending", "chain", "--drop"]) == 0
    assert cli.main(["workflow", "pending", "chain", "--drop"]) == 1
    assert cli.main(["workflow", "step", "set", "chain", "build", "--title", "Make", "--now"]) == 0
    assert cli.main(["workflow", "rm", "chain", "--scope", "user"]) == 1
    assert cli.main(["workflow", "rm", "chain", "--scope", "user", "--force"]) == 0
    assert workflows.status(store, third["id"])["state"] == "running"  # it goes on with its copy


def test_a_step_runs_as_the_role_it_names_only_when_the_project_s_team_has_it(git_repo, store: Store, kicks):
    from cauce import repo, teams

    with pytest.raises(workflows.WorkflowError, match="role is a role's name"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "role": "Bad Name"}]})
    _save({"name": "roled", "steps": [
        {"id": "audit", "prompt": "Review, without changing any file, the login", "kind": "review-critical",
         "role": "security-auditor"},
        {"id": "fix", "prompt": "Fix the login", "kind": "implement", "role": "security-auditor"}]}, git_repo)
    started = workflows.start(store, "roled", git_repo)
    first = store.get_task(started["queued"][0])
    assert json.loads(first["options"])["step_role"] == "security-auditor"
    roles_seen: list[str] = []

    def runner(text, where, store, options, **kw):
        report = run(text, where, store, options, registry={}, launcher=lambda spec: ok(), adapters=[],
                     classifier=kind("review-critical"), **kw)
        roles_seen.append(report.plan.role.name)
        return report

    teams.create("narrow", ["reviewer", "implementer"])
    teams.use(repo.key(git_repo.resolve()), "narrow")
    flow.work(store, runner=runner)
    # not in the project's team: the team picks, and the plan says why
    assert roles_seen[0] == "reviewer"
    reasons = store.last_event(first["id"], "planned")["data"]["reasons"]
    assert any("names security-auditor, which is not in the narrow team" in r for r in reasons)


def test_a_step_role_in_the_team_is_the_one_it_runs_as(git_repo, store):
    from cauce.orchestrate import Options

    dry = run("x", git_repo, store, Options(dry_run=True, step_role="security-auditor"), registry={},
              classifier=kind("review-critical"))
    assert dry.plan.role.name == "security-auditor"
    assert any("the workflow step runs as security-auditor" in r for r in dry.plan.reasons)
    assert Options(step_role="x").stored()["step_role"] == "x"


# --- context boundaries --------------------------------------------------------------


def test_a_review_step_is_blind_and_the_step_after_it_gets_its_findings_as_records(git_repo, store: Store, kicks):
    _save(CHAIN, git_repo)
    workflows.start(store, "chain", git_repo, {"goal": "the export"})
    found = ({"where": "export.py:12", "what": "drops the last row", "evidence_class": "deterministic",
              "cause": "introduced", "proof": "python probe.py"},)
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it, all green"), WorkerResult(True, None, "one problem", "read it",
                                                                            findings=found), ok("polished")], seen))
    _build, check, polish = seen
    # The review reads the change, never what the build said of it.
    assert "built it, all green" not in check["prompt"] and "git diff" in check["prompt"]
    assert "Its findings:" in polish["prompt"] and "export.py:12: drops the last row" in polish["prompt"]
    # A step further back is still summarized, without its findings.
    assert "- build (task #" in polish["prompt"]


def test_a_review_with_no_change_to_read_gets_what_came_before(git_repo, store: Store, kicks):
    _save({"name": "critique", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "Plan {goal}", "kind": "plan"},
        {"id": "critique", "prompt": "Review the plan the step before wrote", "kind": "review-critical",
         "needs": ["plan"]}]}, git_repo)
    workflows.start(store, "critique", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("step 1: split the writer"), ok("the plan holds")], seen))
    # A plan keeps no branch: there is no diff to read, so the plan itself is what it reviews.
    assert "step 1: split the writer" in seen[1]["prompt"] and "git diff" not in seen[1]["prompt"]


def test_the_bundled_feature_builds_with_its_tests_and_a_clean_review_ends_it(git_repo, store: Store, kicks):
    defn = workflows.load("feature", git_repo, "bundled")
    assert [s["id"] for s in defn["steps"]] == ["build", "review", "refute", "fix", "recheck"]
    assert "with the tests that prove it works" in defn["steps"][0]["prompt"]
    started = workflows.start(store, "feature", git_repo, {"goal": "the export"})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems")], seen))
    # Two workers: the build and its review. The ledger steps had nothing to decide.
    assert len(seen) == 2 and workflows.status(store, started["id"])["state"] == "done"
    assert workflows.load("bugfix", git_repo, "bundled")["steps"][0]["kind"] == "debug-repro"
    # review-fix reads code with no change under review: its findings are not excused as pre-existing.
    assert "so `unknown`" in workflows.load("review-fix", git_repo, "bundled")["steps"][0]["prompt"]


def test_a_run_says_what_it_takes_before_the_first_try(git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save({"name": "change", "inputs": ["goal", "check"], "steps": [
        {"id": "build", "prompt": "Build {goal}; then {check}", "kind": "implement", "memory": ["topic:conventions"]},
        {"id": "review", "prompt": "Review {goal}", "kind": "review-routine", "needs": ["build"]}]}, git_repo)
    # The listing says what a run asks for.
    assert cli.main(["workflow", "list"]) == 0
    listed = capsys.readouterr().out
    assert "inputs: goal, check · memory: topic:conventions" in listed
    # Text without a name fills the one input left; with two left, the error names them all.
    assert cli.main(["workflow", "run", "change", "the export"]) == 1
    assert "--input goal='…' --input check='…'" in capsys.readouterr().err
    # A dry run checks everything and queues nothing; the memory it would start without is said.
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test", "--dry-run",
                     "--verify", "npm run lint"]) == 0
    dry = capsys.readouterr().out
    assert "nothing is queued" in dry and "goal: the export" in dry and "holds no note in this project yet" in dry
    assert "build (implement), checked by `npm run lint`" in dry and "review (review-routine) after build\n" in dry
    assert store.list_runs() == []
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test", "--dry-run",
                     "--verify", "npm run lint", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert [s["verify"] for s in planned["steps"]] == ["npm run lint", None] and planned["gaps"]
    # The run's check goes to the step that writes, never to the review.
    assert cli.main(["workflow", "run", "change", "the export", "--input", "check=npm test",
                     "--verify", "npm run lint"]) == 0
    assert "note: step 'build'" in capsys.readouterr().out
    (build,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(build["options"])["verify"] == "npm run lint"
    assert workflows.step_check({"kind": "review-routine"}, {"verify": "npm run lint"}) is None


def test_a_flag_a_command_does_not_take_says_what_was_meant(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["workflow", "run", "change", "the goal", "two words", "--allw", "x"])
    assert stop.value.code == 2
    said = capsys.readouterr().err
    assert "--allw: did you mean --allow?" in said and "--input name=value" in said
    with pytest.raises(SystemExit):
        cli.main(["run", "fix it", "--input", "goal=x"])
    assert "--input is taken by cauce workflow run, not by `cauce run`" in capsys.readouterr().err


def test_the_status_says_how_far_each_run_got_in_a_line(git_repo, store: Store, kicks, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    _save(CHAIN, git_repo)
    brief = "Build the export of every order " + "with all its details " * 20
    started = workflows.start(store, "chain", git_repo, {"goal": brief})
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built it"), ok("no problems"), ok("polished")], seen))
    shown = workflows.status(store, started["id"], detail=True)
    build = shown["steps"][0]
    # A done step says the commit it kept and the files it touched, from git and its events.
    assert build["attempt"] == 1 and build["files"] == ["step1.txt"] and build["commit"].split()[1:]
    assert cli.main(["workflow", "status"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith(f"#{started['id']} chain — done") and "step polish (3/3)" in line and "1 file(s)" in line
    assert brief not in line
    # One run step by step: its input on one line unless asked for whole.
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    out = capsys.readouterr().out
    assert "goal: Build the export" in out and brief.strip() not in out and "attempt 1 at" in out
    assert cli.main(["workflow", "status", str(started["id"]), "--full"]) == 0
    assert brief.strip() in capsys.readouterr().out


def test_the_status_says_what_a_held_back_step_waits_for(git_repo, store: Store, kicks, capsys, monkeypatch):
    from cauce import dispatch

    monkeypatch.chdir(git_repo)
    _save(CHAIN, git_repo)
    other = store.enqueue("rewrite the exporter", repo=str(git_repo.resolve()), cwd=str(git_repo))
    store.claim(other["id"])
    started = workflows.start(store, "chain", git_repo, {"goal": "the export"})
    flow.runnable(store, None, parallel=True,
                  decide=lambda task, ahead: dispatch.Decision(False, f"both change the exporter #{other['id']}"))
    line = f"waits for #{other['id']}: both change the exporter #{other['id']}"
    assert workflows.status(store, started["id"])["steps"][0]["waits"] == line
    assert cli.main(["workflow", "status"]) == 0
    assert line in capsys.readouterr().out
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    assert line in capsys.readouterr().out


def test_a_running_step_says_its_attempt_and_the_files_it_touched_so_far(git_repo, store: Store):
    from cauce import isolate
    from cauce.store import home

    from .conftest import git

    task = store.create_task("build it", status="running", source="queue", cwd=str(git_repo))
    ws = isolate.prepare(git_repo, task["id"], home())
    store.add_event(task["id"], "attempt_started", seq=2, cell="sonnet/high", base=ws.base, links=list(ws.links))
    # What the worker committed counts as much as what it has not yet.
    (ws.path / "export.py").write_text("print('rows')\n")
    git(ws.path, "add", "export.py")
    git(ws.path, "commit", "-qm", "export")
    (ws.path / "more.py").write_text("x = 1\n")
    found = workflows.progress(store, {"cwd": str(git_repo)}, store.get_task(task["id"]))
    assert found["attempt"] == 2 and found["files"] == ["export.py", "more.py"] and "phase" in found


def test_the_run_check_reaches_a_step_with_no_kind_once_it_is_known_to_write(git_repo, store: Store):
    from cauce.orchestrate import Options

    # A step classified into a writing kind runs the run's check, and a red one is no pass.
    red = run("add the export", git_repo, store, Options(run_verify="exit 1", max_attempts=1), registry={},
              launcher=_worker([ok("built it")], []), classifier=kind("implement"), adapters=[])
    assert red.status == "failed" and "`exit 1` exited 1" in store.attempts(red.task_id)[0]["summary"]
    # One classified into a reading kind never gets it.
    read = run("explain the export", git_repo, store, Options(run_verify="exit 1"), registry={},
               launcher=_worker([ok("it works so")], []), classifier=kind("explore"), adapters=[])
    assert read.status == "done"


def test_a_kindless_step_carries_the_run_check_and_the_dry_run_says_so(git_repo, store: Store, kicks, capsys,
                                                                       monkeypatch):
    monkeypatch.chdir(git_repo)
    _save({"name": "plain", "inputs": ["goal"], "steps": [{"id": "do", "prompt": "{goal}",
                                                           "memory": ["topic:style", "topic:contracts"]}]}, git_repo)
    assert cli.main(["workflow", "run", "plain", "the export", "--verify", "npm test", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "checked by `npm test` if it writes" in out
    # An alias reads as its topic; a topic the project lacks is added before a note can fill it.
    assert "topic 'style' holds no note" in out and "`cauce note --topic conventions" in out
    assert "cauce notes topic add contracts" in out
    assert cli.main(["workflow", "run", "plain", "the export", "--verify", "npm test"]) == 0
    (task,) = store.run_tasks(store.list_runs()[0]["id"])
    assert json.loads(task["options"])["run_verify"] == "npm test"
    capsys.readouterr()
    assert cli.main(["workflow", "status", "--repo", str(git_repo / "nowhere")]) == 0
    assert capsys.readouterr().out.splitlines() == ["no workflow run here; `cauce workflow list` shows what can run"]


def test_the_line_of_a_run_names_the_step_that_waits_on_the_person():
    from cauce import workflow_cli

    line = workflow_cli._run_line({"id": 7, "name": "dual", "state": "waits on you", "cost_usd": 1.0,
                                   "waiting": ["a"], "steps": [
                                       {"id": "a", "state": "failed", "task": 10},
                                       {"id": "b", "state": "running", "task": 11, "attempt": 1}]})
    assert "step a (1/2): failed #10" in line


def test_a_resumed_step_counts_the_files_of_its_final_ending_not_an_attempts(git_repo, store: Store):
    from cauce import workflow_cli

    task = store.create_task("build it", status="interrupted", source="queue", cwd=str(git_repo))
    for _ in range(2):
        store.add_event(task["id"], "finished", status="interrupted", reason="no sign of life",
                        stop={"cause": "died", "reason": "no sign of life"})
    store.update_task(task["id"], status="done")
    files = [f"src/f{i}.ts" for i in range(106)]
    # The run kept the head of the list; the count is the whole of it.
    store.add_event(task["id"], "finished", status="done", branch="cauce/task-1", changed=files[:100], changed_n=106)
    found = workflows.progress(store, {"cwd": str(git_repo)}, store.get_task(task["id"]))
    assert found["files_n"] == 106 and len(found["files"]) == 100
    assert "106 file(s): src/f0.ts, src/f1.ts, src/f2.ts +103" in workflow_cli._step_line(
        {"state": "done", "task": task["id"], **found})
    # Without the count (an older ending) the list is the count.
    assert "3 file(s)" in workflow_cli._step_line({"state": "done", "files": ["a", "b", "c"]})
