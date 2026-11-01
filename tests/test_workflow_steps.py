"""A workflow's steps that choose (`when`), ask the person (`ask`) or run another workflow
(`workflow`); its graph as text (`render`); a change refused on a stale revision (`expect`)."""
from __future__ import annotations

import json
import threading

import pytest

from locks import cli, flow, stops, workflows
from locks.launch import WorkerResult
from locks.store import Store

from .test_orchestrate import ok
from .test_workflows import _runner, _save

FOUND = {"where": "app.py:3", "what": "drops the last item", "evidence_class": "deterministic",
         "cause": "introduced", "proof": "python probe.py"}


def reviewed(found=()) -> WorkerResult:
    return WorkerResult(True, None, "reviewed", "read the diff", cost_usd=0.1, findings=tuple(found))


BRANCHING = {"name": "branching", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "review", "prompt": "Review, without changing any file, {goal}", "kind": "review-routine"},
    {"id": "fix", "prompt": "Fix what the review found in {goal}", "kind": "implement", "when": {"review": "findings"}},
    {"id": "note", "prompt": "Write down that {goal} passed review", "kind": "docs", "needs": ["review"],
     "when": {"review": "clean"}},
    {"id": "ship", "prompt": "Prepare {goal} to ship", "kind": "implement", "needs": ["fix", "note"]},
]}


# --- definitions ---------------------------------------------------------------------


def test_when_names_a_step_it_needs_and_an_outcome_its_ending_can_say():
    defn = workflows.normalize(BRANCHING)
    assert defn["steps"][2]["when"] == {"review": "findings"}
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize({"name": "x", "steps": [
            {"id": "a", "prompt": "a"},
            {"id": "b", "prompt": "b", "when": {"a": "maybe"}},
            {"id": "c", "prompt": "c", "needs": ["b"], "when": {"a": "clean"}},
            {"id": "d", "prompt": "d", "needs": ["c"], "when": "clean"}]})
    said = str(err.value)
    assert "when 'a' is one of findings, clean, changed, unchanged, not 'maybe'" in said
    assert "when names 'a', which it does not need" in said and "step 'd': when maps steps it needs" in said


def test_a_question_has_its_options_and_no_worker_s_fields():
    defn = workflows.normalize({"name": "q", "inputs": ["goal"], "steps": [
        {"id": "ask", "ask": "Ship {goal} behind a flag?", "options": ["flag", "direct"]},
        {"id": "flagged", "prompt": "Ship {goal} behind a flag", "when": {"ask": "flag"}}]})
    assert defn["steps"][0]["options"] == ["flag", "direct"]
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize({"name": "q", "steps": [
            {"id": "ask", "ask": "Which?", "options": ["only"], "kind": "implement", "prompt": "x"},
            {"id": "b", "prompt": "b", "when": {"ask": "other"}},
            {"id": "both", "ask": "x", "options": ["a", "b"], "workflow": "bugfix"}]})
    said = str(err.value)
    assert "it takes no kind, prompt" in said and "options are 2 to 6 different answers" in said
    assert "'ask' asks the person; when takes one of its options ['only'], not 'other'" in said
    assert "asks the person or runs a workflow, not both" in said


def test_a_sub_workflow_step_names_a_workflow_and_maps_its_inputs():
    with pytest.raises(workflows.WorkflowError) as err:
        workflows.normalize({"name": "p", "inputs": ["goal"], "steps": [
            {"id": "sub", "workflow": "Bad Name", "with": {"bug": "{nope}"}, "kind": "implement"}]})
    said = str(err.value)
    assert "takes no kind" in said and "workflow is the name of a saved workflow" in said and "{nope}" in said
    # An input only a question or a sub-workflow names is no check input.
    defn = workflows.normalize({"name": "p", "inputs": ["goal", "check"], "steps": [
        {"id": "sub", "workflow": "bugfix", "with": {"bug": "{goal}"}},
        {"id": "do", "prompt": "do it", "verify": "{check}"}]})
    assert workflows.check_inputs(defn) == ["check"]


# --- runs: choosing ------------------------------------------------------------------


def test_a_review_with_findings_runs_the_fix_and_skips_the_note(git_repo, store: Store, kicks):
    _save(BRANCHING, git_repo)
    started = workflows.start(store, "branching", git_repo, {"goal": "the export"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built"), reviewed([FOUND]), ok("fixed"), ok("ready")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done"
    assert {s["id"]: s.get("skipped", "") for s in status["steps"]}["note"].startswith(
        "its condition review is 'clean' did not hold: review was 'findings'")
    assert [s["state"] for s in status["steps"]] == ["done"] * 5
    # Four workers ran: build, review, fix and ship; the note was skipped with no worker.
    assert len(seen) == 4 and "Fix what the review found" in seen[2]["prompt"]
    # The step after the skipped one starts from the fix's work, and the session hears of the run once.
    assert "step3.txt" in seen[3]["files"]
    assert [t["id"] for t in store.unreported("s1")] == [status["steps"][-1]["task"]]


def test_a_clean_review_skips_the_fix(git_repo, store: Store, kicks):
    _save(BRANCHING, git_repo)
    started = workflows.start(store, "branching", git_repo, {"goal": "the export"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built"), reviewed(), ok("noted"), ok("ready")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][2].get("skipped")
    assert "Write down that the export passed review" in seen[2]["prompt"]
    assert workflows.outcome(store, store.get_task(status["steps"][1]["task"])) == {
        "answer": None, "findings": 0, "changed": 0}


# --- runs: asking the person -----------------------------------------------------------

ASKING = {"name": "asking", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "how", "ask": "Ship {goal} behind a flag or directly?", "options": ["flag", "direct"]},
    {"id": "flagged", "prompt": "Put {goal} behind a flag", "kind": "implement", "when": {"how": "flag"}},
    {"id": "direct", "prompt": "Ship {goal} directly", "kind": "implement", "needs": ["how"],
     "when": {"how": "direct"}},
]}


def test_a_question_waits_on_the_person_and_their_answer_chooses_the_branch(git_repo, store: Store, kicks):
    _save(ASKING, git_repo)
    started = workflows.start(store, "asking", git_repo, {"goal": "the export"}, session_id="s1")
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built")], seen))
    status = workflows.status(store, started["id"])
    asked = status["steps"][1]
    assert asked["state"] == "blocked" and asked["options"] == ["flag", "direct"]
    assert status["waiting"] == ["how"] and len(seen) == 1  # no worker ran the question
    task = store.get_task(asked["task"])
    stop = stops.of(store, task)
    assert stop.cause == "question" and stops.for_person(stop) and stop.reason.startswith("Ship the export")
    line = f"locks workflow answer {started['id']} how <flag|direct>"
    assert stops.next_step(task["id"], stop) == line and stops.view(store, task)["next"] == line
    assert stops.resume_args(task["id"], stops.view(store, task)) is None
    # The session hears of the question, as a step that waits on the person.
    assert asked["task"] in [t["id"] for t in store.unreported("s1")]
    with pytest.raises(workflows.WorkflowError, match="is no answer to 'how': one of flag, direct"):
        workflows.answer(store, started["id"], "how", "maybe")
    with pytest.raises(workflows.WorkflowError, match="asks nothing"):
        workflows.answer(store, started["id"], "build", "flag")
    done = workflows.answer(store, started["id"], "how", "Flag ")
    assert done["answer"] == "flag" and len(done["queued"]) == 1
    assert asked["task"] not in [t["id"] for t in store.unreported("s1")]  # answered: nothing to tell
    with pytest.raises(workflows.WorkflowError, match="waits for no answer"):
        workflows.answer(store, started["id"], "how", "direct")
    flow.work(store, runner=_runner([ok("flagged")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][1]["answer"] == "flag"
    assert status["steps"][3].get("skipped") and "Put the export behind a flag" in seen[1]["prompt"]


def test_a_question_is_answered_never_resumed(git_repo, store: Store, kicks, capsys, monkeypatch):
    _save(ASKING, git_repo)
    started = workflows.start(store, "asking", git_repo, {"goal": "the export"}, session_id="s1")
    flow.work(store, runner=_runner([ok("built")], []))
    asked = workflows.status(store, started["id"])["steps"][1]["task"]
    assert cli.main(["resume", str(asked)]) == 1
    assert f"answer it with `locks workflow answer {started['id']} how <flag|direct>`" in capsys.readouterr().err
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "status", str(started["id"])]) == 0
    assert "asks you: flag | direct" in capsys.readouterr().out
    assert cli.main(["workflow", "answer", str(started["id"]), "how", "direct"]) == 0
    assert "how answered direct; queued #" in capsys.readouterr().out
    monkeypatch.setenv("LOCKS_WORKER_TASK", "9")
    assert cli.main(["workflow", "answer", str(started["id"]), "how", "direct"]) != 0


# --- runs: another workflow as a step --------------------------------------------------

INNER = {"name": "inner", "inputs": ["bug"], "steps": [
    {"id": "repair", "prompt": "Fix {bug}", "kind": "implement"},
    {"id": "tidy", "prompt": "Tidy after the fix of {bug}", "kind": "implement"},
]}
OUTER = {"name": "outer", "inputs": ["goal"], "steps": [
    {"id": "build", "prompt": "Build {goal}", "kind": "implement"},
    {"id": "repair", "workflow": "inner", "with": {"bug": "what broke in {goal}"}},
    {"id": "ship", "prompt": "Ship {goal}", "kind": "implement"},
]}


def test_a_sub_workflow_runs_on_the_work_before_it_and_the_run_goes_on_from_its_branch(git_repo, store: Store,
                                                                                       kicks):
    _save(INNER, git_repo)
    _save(OUTER, git_repo)
    started = workflows.start(store, "outer", git_repo, {"goal": "the export"}, session_id="s1")
    assert set(json.loads(store.get_run(started["id"])["options"])["subflows"]) == {"inner"}
    seen: list[dict] = []
    flow.work(store, runner=_runner([ok("built"), ok("repaired"), ok("tidied"), ok("shipped")], seen))
    status = workflows.status(store, started["id"])
    assert status["state"] == "done", status
    child = status["steps"][1]["subrun"]
    inner = workflows.status(store, child)
    assert inner["state"] == "done" and inner["inputs"] == {"bug": "what broke in the export"}
    # The child started from the build; ship started from the child's last step.
    assert "Fix what broke in the export" in seen[1]["prompt"] and "step1.txt" in seen[1]["files"]
    assert {"step1.txt", "step2.txt", "step3.txt"} <= set(seen[3]["files"])
    assert status["steps"][1]["branch"] == inner["branch"]
    assert status["steps"][1]["cost_usd"] == pytest.approx(0.2)
    # The session hears of the outer run's end once, never of the child's own.
    assert [t["id"] for t in store.unreported("s1")] == [status["steps"][2]["task"]]
    # A sub-workflow's step is never resumed as a task.
    assert cli._not_resumable(store.get_task(status["steps"][1]["task"])).startswith("runs workflow run #")


def test_a_sub_workflow_is_frozen_checked_and_cancelled_with_its_run(git_repo, store: Store, kicks):
    _save(INNER, git_repo)
    _save(OUTER, git_repo)
    started = workflows.start(store, "outer", git_repo, {"goal": "the export"}, session_id="s1")
    flow.work(store, runner=_runner([ok("built")], []), max_tasks=1)
    status = workflows.status(store, started["id"])
    child = status["steps"][1]["subrun"]
    assert status["state"] == "running" and "waits for run" in (status["steps"][1].get("waits") or "")
    workflows.cancel(store, started["id"])
    assert store.get_run(child)["status"] == "cancelled"
    assert all(t["status"] == "cancelled" for t in store.run_tasks(child))
    # One that calls itself, a missing one and one missing an input are refused before a run starts.
    loop = {"name": "loop", "inputs": ["goal"], "steps": [{"id": "again", "workflow": "loop",
                                                          "with": {"goal": "{goal}"}}]}
    _save(loop, git_repo)
    with pytest.raises(workflows.WorkflowError, match="already running it"):
        workflows.prepare(store, "loop", git_repo, {"goal": "x"})
    _save({"name": "short", "inputs": ["goal"], "steps": [{"id": "s", "workflow": "inner"}]}, git_repo)
    with pytest.raises(workflows.WorkflowError, match="without bug"):
        workflows.prepare(store, "short", git_repo, {"goal": "x"})
    _save({"name": "gone", "inputs": ["goal"], "steps": [{"id": "s", "workflow": "nowhere"}]}, git_repo)
    with pytest.raises(workflows.WorkflowError):
        workflows.prepare(store, "gone", git_repo, {"goal": "x"})
    _save({"name": "extra", "inputs": ["goal"], "steps": [
        {"id": "s", "workflow": "inner", "with": {"bug": "{goal}", "other": "x"}}]}, git_repo)
    with pytest.raises(workflows.WorkflowError, match="which it does not take"):
        workflows.prepare(store, "extra", git_repo, {"goal": "x"})


def test_sub_workflows_nest_only_so_deep(git_repo, store: Store, monkeypatch):
    monkeypatch.setattr(workflows, "SUBFLOW_DEPTH", 1)
    _save(INNER, git_repo)
    _save({"name": "middle", "inputs": ["bug"], "steps": [{"id": "s", "workflow": "inner", "with": {"bug": "{bug}"}}]},
          git_repo)
    _save({"name": "top", "inputs": ["goal"], "steps": [{"id": "s", "workflow": "middle",
                                                         "with": {"bug": "{goal}"}}]}, git_repo)
    with pytest.raises(workflows.WorkflowError, match="nest 1 at most"):
        workflows.prepare(store, "top", git_repo, {"goal": "x"})


# --- the graph as text ----------------------------------------------------------------


def test_a_workflow_renders_as_a_mermaid_graph_with_its_conditions():
    defn = workflows.normalize({**ASKING, "steps": [*ASKING["steps"], {
        "id": "sub", "workflow": "inner", "with": {"bug": "{goal}"}, "needs": ["flagged", "direct"]}]})
    graph = workflows.render(defn)
    assert graph.startswith("flowchart TD\n")
    assert 's1{"how<br/><i>asks: flag / direct</i>"}' in graph
    assert 's4[["sub<br/><i>runs inner</i>"]]' in graph
    assert 's1 -->|"flag"| s2' in graph and "s0 --> s1" in graph
    md = workflows.render_markdown(defn)
    assert "```mermaid" in md and "- **how** (asks: flag / direct): Ship {goal} behind a flag or directly?" in md
    quoted = workflows.render(workflows.normalize({"name": "q", "steps": [
        {"id": "a", "title": 'say "hi" <b>', "prompt": "x", "kind": "explore"},
        {"id": "b", "prompt": "y", "ledger": "refute"}]}))
    assert 's0(["say #quot;hi#quot; #lt;b#gt;' in quoted and 's1{{"' in quoted


def test_a_run_renders_with_each_step_s_state(git_repo, store: Store, kicks, capsys, monkeypatch):
    _save(ASKING, git_repo)
    started = workflows.start(store, "asking", git_repo, {"goal": "the export"}, session_id="s1")
    flow.work(store, runner=_runner([ok("built")], []))
    status = workflows.status(store, started["id"])
    graph = workflows.render(workflows.normalize(json.loads(store.get_run(started["id"])["definition"])), status)
    assert "classDef done" in graph and "class s0 done" in graph and "class s1 stopped" in graph
    assert f"blocked #{status['steps'][1]['task']}" in graph
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "render", "--run", str(started["id"]), "--markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"## asking — run #{started['id']}") and "```mermaid" in out
    assert cli.main(["workflow", "render", "asking"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TD")
    assert cli.main(["workflow", "render"]) == 1
    assert cli.main(["workflow", "render", "--run", "999"]) == 1


# --- a change on a stale revision --------------------------------------------------------


def test_a_change_made_on_a_revision_that_moved_is_refused(git_repo, capsys, monkeypatch):
    path = _save({"name": "mine", "inputs": ["goal"], "steps": [{"id": "do", "prompt": "{goal}"}]}, git_repo)
    read = workflows.revision(path)
    assert read.startswith("sha256:") and len(read) == len("sha256:") + 16
    assert workflows.revision(path.parent / "nothing.json") == "none"
    workflows.edit("mine", git_repo, lambda d: d["steps"].append({"id": "more", "prompt": "more"}), expect=read)
    with pytest.raises(workflows.WorkflowError, match="it changed since you read it"):
        workflows.edit("mine", git_repo, lambda d: d.update(description="late"), expect=read)
    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "show", "mine"]) == 0
    now = workflows.revision(path)
    assert f"revision {now}" in capsys.readouterr().out
    assert cli.main(["workflow", "step", "set", "mine", "more", "--prompt", "less", "--expect", read]) == 1
    assert "changed since you read it" in capsys.readouterr().err
    assert cli.main(["workflow", "step", "set", "mine", "more", "--prompt", "less", "--expect", now]) == 0
    raw = path.parent / "raw.json"
    raw.write_text(json.dumps({"name": "mine", "inputs": ["goal"], "steps": [{"id": "do", "prompt": "{goal}"}]}))
    assert cli.main(["workflow", "save", str(raw), "--replace", "--expect", now]) == 1
    assert cli.main(["workflow", "save", str(raw), "--replace", "--expect", workflows.revision(path)]) == 0


# --- the board stops when nobody looks ----------------------------------------------------


def test_the_board_stops_once_its_last_viewer_left_and_never_before_one_came(tmp_path):
    from locks.ui import server

    ui = server.UIServer(0, tmp_path)
    try:
        assert ui.idle_for() is None  # nobody came yet: it stays up
        ui.seen(1)
        assert ui.idle_for() is None  # a stream is open
        ui.seen(-1)
        assert ui.idle_for() is not None and ui.idle_for() < 5
        serving = threading.Thread(target=ui.serve_forever, daemon=True)
        serving.start()
        ui.exit_when_idle(0.0, every=0.01)
        serving.join(5)
        assert not serving.is_alive() and ui.stop_event.is_set()
    finally:
        ui.server_close()
