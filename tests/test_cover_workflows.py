"""The corners of workflows, workflow_cli, after, loops and loop_cli the scenario tests do not
reach: each test asserts what the code returns, records or prints there."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pytest

from cauce import after, cli, loops, stops, workflows
from cauce import workflow_cli as wc
from cauce.store import Store

from .conftest import git

# --- after ---------------------------------------------------------------------------------


def _held(store: Store, wanted: dict, *, repo: str = "r", cwd: str | None = None, **fields) -> dict:
    return store.create_task("held work", status="queued", source="queue", repo=repo, cwd=cwd,
                             options=json.dumps({after.KEY: wanted}), **fields)


def test_a_hold_unreadable_options_hold_nothing():
    assert after.hold({"options": "not json"}) is None
    assert after.hold({"options": "[1, 2]"}) is None  # a list has no .get
    assert after.held({"status": "queued", "options": "{oops"}) is False
    assert after._part_id({"options": "{oops"}) is None
    assert after._part_id({"options": "[]"}) is None


def test_a_caveat_names_the_other_repository_and_this_one_s_run_of_the_same_number(store: Store):
    theirs = store.create_task("their work", status="done", source="cauce", repo="other")
    assert after.caveat(store, {"task": theirs["id"]}, "other") is None  # this repository's own
    said = after.caveat(store, {"task": theirs["id"]}, "mine")
    assert said == (f"#{theirs['id']} is a task in other: this starts on this repository's base, and reads "
                    "what that work kept where it lives")
    # a run of this repository with the same number is named as the `--after run:N` it may have meant
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="mine")
    while run["id"] < theirs["id"]:
        run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="mine")
    assert run["id"] == theirs["id"]
    said = after.caveat(store, {"task": theirs["id"]}, "mine")
    assert f"; run #{run['id']} of this repository is `--after run:{run['id']}`" in said
    # a run in another repository
    elsewhere = store.create_run(name="w", scope="user", source="x", definition="{}", repo=None)
    said = after.caveat(store, {"run": elsewhere["id"]}, "mine")
    assert said.startswith(f"run #{elsewhere['id']} is a run in no repository: this starts")
    assert after.caveat(store, {"run": 9999}, "mine") is None


def test_held_work_on_a_task_in_another_repository_that_stopped_says_how(store: Store):
    other = store.create_task("x" * 120, status="blocked", source="cauce", repo="other")
    stops.record(store, other["id"], stops.Stop("review", "the verifier found a blocker"))
    task = _held(store, {"task": other["id"]}, repo="mine")
    line = after.waiting(store, task)
    title = store.get_task(other["id"])["title"]
    shown = " ".join(title.split())
    shown = shown if len(shown) <= 80 else shown[:79] + "…"
    assert line == (f"waits for task #{other['id']} in other ({shown}), which stopped as review: a resume of it "
                    "to a pass releases this")


def test_another_worktree_of_the_same_repository_is_the_same_repository(git_repo, tmp_path):
    wt = tmp_path / "wt"
    git(git_repo, "worktree", "add", "-q", "-b", "side", str(wt))
    # With no remote each checkout is keyed by its path: the shared git store still makes them one.
    assert after._elsewhere(str(git_repo), wt) is None
    assert after._elsewhere(str(wt), git_repo) is None
    assert after.reads(("side", [], "#1", str(git_repo)), wt) is None
    git(git_repo, "remote", "add", "origin", "https://example.invalid/team/app.git")
    assert after._elsewhere(str(git_repo), wt) is None
    assert after._elsewhere(None, wt) is None
    assert after._elsewhere(str(git_repo), None) is None
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    assert after._elsewhere(str(other), wt) == other


def test_a_task_released_meanwhile_is_not_released_again(store: Store, monkeypatch):
    first = store.create_task("first", status="done", source="cauce", repo="r")
    task = _held(store, {"task": first["id"]})
    stale = dict(task)
    # Another process released it between the listing and the write: the transaction reads it again.
    store.update_task(task["id"], options="{}")
    monkeypatch.setattr(after, "behind", lambda s, w: [stale])
    assert after.landed(store, first["id"], "main") == []
    assert store.last_event(task["id"], "released") is None

    real = store.list_tasks
    monkeypatch.setattr(store, "list_tasks", lambda **kw: [stale] if kw.get("status") == ["queued"] else real(**kw))
    assert after.release(store, task=first) == []
    assert store.last_event(task["id"], "released") is None


def test_a_held_step_outside_a_checkout_has_no_head(store: Store, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert after._now({"cwd": str(plain), "workflow_run": 1}) is None
    assert after._now({"cwd": str(plain), "workflow_run": None}) is None
    assert after._now({"cwd": None, "workflow_run": 1}) is None


def test_releasing_a_step_frees_its_held_run_with_what_it_reads_and_where_it_is_now(store: Store, tmp_path):
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="r",
                           options=json.dumps({after.KEY: {"task": 1}, "add_dirs": ["/kept"]}))
    task = _held(store, {"task": 1}, workflow_run=run["id"], workflow_step="a")
    after._free(store, task, {"task": 1}, None, "why", head=None, read="/other/repo")
    freed = json.loads(store.get_run(run["id"])["options"])
    assert after.KEY not in freed
    assert freed["add_dirs"] == ["/kept", "/other/repo"]
    assert "origin" not in freed and "base" not in freed  # nothing to start on, and no head read
    assert json.loads(store.get_task(task["id"])["options"])["add_dirs"] == ["/other/repo"]
    released = store.last_event(task["id"], "released")["data"]
    assert released == {"after": {"task": 1}, "base": None, "why": "why"}


# --- loops ---------------------------------------------------------------------------------


def _a_loop(cwd, **over):
    from .test_loops import _loop

    return _loop(cwd, **over)


def test_a_diary_skips_lines_that_are_no_round(tmp_path):
    _a_loop(tmp_path)
    loops._write("leads", {"round": 1, "run": 5})
    path = loops._file("leads", ".diary.jsonl")
    with path.open("a", encoding="utf-8") as out:
        out.write("[1, 2]\n{\"round\": \"two\"}\nnot json\n")
    loops._write("leads", {"round": 1, "ended": "t"})
    assert loops.diary("leads") == [{"round": 1, "run": 5, "ended": "t"}]


def test_an_unmeasured_round_counts_for_no_arm_and_a_row_of_no_arm_is_left_out(tmp_path):
    loop = _a_loop(tmp_path, metric="x", variants=["a", "b"], trial=1)
    rows = [{"round": 1, "arm": 0, "before": None, "after": 3}]  # never measured before it
    assert loops._gain(loop, None, 3) is None
    assert loops.arm(loop, rows, {}) == (0, "on trial (0 of 1 measured rounds)")
    rows += [{"round": 2, "arm": "zero", "before": 1, "after": 9}, {"round": 3, "arm": 7, "before": 1, "after": 9}]
    assert loops.judge(loop, rows) is None  # neither row is an arm's
    rows += [{"round": 4, "arm": 0, "before": 1, "after": 2}, {"round": 5, "arm": 1, "before": 2, "after": 5}]
    assert loops.judge(loop, rows) == (1, {0: 1.0, 1: 3.0})


def test_the_best_before_the_stale_window_skips_unmeasured_and_worse_rounds(tmp_path):
    loop = _a_loop(tmp_path, metric="x", stale=1)
    now = datetime(2026, 10, 26, 8, 0).astimezone()
    rows = [{"round": 1, "before": 5, "after": None, "ended": "t"},  # unmeasured: no best
            {"round": 2, "after": 4, "ended": "t"},  # worse than 5: not the best
            {"round": 3, "after": 6, "ended": "t"}]
    assert loops.stop_reason(loop, rows, now=now) == ""  # 6 beats the best before it, 5
    rows[2]["after"] = 5
    assert loops.stop_reason(loop, rows, now=now) == "1 round(s) in a row did not improve its metric"


def test_a_round_s_summary_skips_steps_that_said_nothing_and_names_what_waits_on_the_person(store: Store):
    run = store.create_run(name="w", scope="user", source="x", definition="{}", repo="r")
    store.create_task("a", status="done", source="queue", workflow_run=run["id"], workflow_step="plan", result="")
    blocked = store.create_task("b", status="blocked", source="queue", workflow_run=run["id"],
                                workflow_step="send", result="wrote it\nNeeds the person:\n- a price for ACME\n")
    stops.record(store, blocked["id"], stops.Stop("permission", "Bash(curl:*) was refused"), result=False)
    said, needs = loops._round_summary(store, run["id"])
    assert said.startswith("send: wrote it") and "plan:" not in said
    assert needs == ["a price for ACME"]
    assert loops._person_only(store, run["id"]) == f"#{blocked['id']} permission (Bash(curl:*) was refused)"


def test_a_round_adopts_the_strategy_once_every_arm_has_its_trial_and_builds_on_the_last(git_repo, store: Store,
                                                                                          monkeypatch):
    loop = _a_loop(git_repo, metric="x", variants=["short", "long"], trial=1, stale=9)
    loops.set_state("leads", status="running")
    loops._write("leads", {"round": 1, "run": 101, "started": "t", "before": 1.0, "arm": 0})
    loops._write("leads", {"round": 1, "ended": "t", "state": "done", "after": 2.0, "changed_files": 3})
    loops._write("leads", {"round": 2, "run": 102, "started": "t", "before": 2.0, "arm": 1})
    monkeypatch.setattr(workflows, "status", lambda s, run_id: {"state": "done", "cost_usd": 0.5})
    started = []

    def start(store_, name, cwd, inputs, **kw):
        started.append(kw)
        return {"id": 103}

    now = datetime(2026, 10, 26, 8, 0).astimezone()
    assert loops.tick(store, "leads", now=now, start=start, measure_fn=lambda lp: (6.0, "")) == "started"
    st = loops.state("leads")
    assert st["adopted"] == 1 and st["adoption"]["means"] == {"0": 1.0, "1": 4.0}
    second, third = loops.diary("leads")[1:]
    # round 2 changed no file but moved the metric: not empty, and still nothing to build on
    assert second["empty"] is False and second["changed_files"] == 0
    assert third["arm"] == 1 and third["arm_why"] == "adopted" and third["before"] == 6.0
    # round 2 kept nothing, so round 3 builds on round 1's run, the last that changed files
    assert started[0]["after_run"] == 101 and started[0]["options"]["loop"]["arm"] == 1
    assert loops._checkout(str(git_repo)) is True
    plain = git_repo.parent / "plain"
    plain.mkdir()
    assert loops._checkout(str(plain)) is False
    told = loops.summary("leads")
    assert "adopted strategy: long" in told and loop.name in told


def test_a_round_s_brief_is_read_only_for_a_loop_that_exists(tmp_path, store: Store):
    assert loops.round_brief(store, {}) == ""
    assert loops.round_brief(store, {"loop": "leads"}) == ""  # an older run's plain name: no round
    assert loops.round_brief(store, {"loop": {"name": "gone"}}) == ""
    _a_loop(tmp_path, variants=["a", "b"])
    text = loops.round_brief(store, {"loop": {"name": "leads", "round": 2, "arm": 5}})
    assert "This is round 2 of the loop 'leads'" in text and "runs the strategy" not in text  # arm 5 is none
    assert "runs the strategy: b" in loops.round_brief(store, {"loop": {"name": "leads", "round": 2, "arm": 1}})


def test_a_summary_counts_what_waits_past_fifteen_and_names_a_round_stopped_for_the_person(tmp_path):
    _a_loop(tmp_path)
    loops._write("leads", {"round": 1, "run": 1, "started": "t", "before": None})
    loops._write("leads", {"round": 1, "ended": "t", "state": "blocked", "needs": [f"item {n}" for n in range(17)],
                           "person": "#4 permission", "cost_usd": 1.25})
    told = loops.summary("leads").splitlines()
    assert "  waits on the person (17):" in told
    assert "    - round 1: item 14" in told and "    - round 1: item 15" not in told
    assert "    … 2 more: `cauce loop show leads --all`" in told
    assert told[-1] == "  round 1 stopped for the person: #4 permission"
    assert "  cost: USD 1.25" in told


def test_endings_pass_over_a_loop_whose_file_is_broken(tmp_path):
    _a_loop(tmp_path, session_id="s1")
    loops.set_state("leads", status="stopped", why="done", reported=False)
    (loops.folder() / "broken.json").write_text("{")
    said = loops.endings("s1")
    assert len(said) == 1 and said[0].startswith("loop leads (draft, stopped)")
    assert loops.endings("s1") == []


def test_the_cli_takes_named_inputs_and_starts_an_acting_loop_without_the_draft_line(tmp_path, monkeypatch,
                                                                                   capsys):
    from .test_loops import _save as save_workflow

    folder = tmp_path / "f"
    folder.mkdir()
    save_workflow({"name": "two", "inputs": ["goal", "list"], "steps": [{"id": "s", "prompt": "{goal} {list}"}]},
                  folder)
    assert cli.main(["loop", "add", "leads", "--goal", "find clients", "--workflow", "two", "--dir", str(folder),
                     "--input", " list =the catalog"]) == 0
    assert loops.load("leads").inputs == {"list": "the catalog", "goal": "find clients"}
    assert cli.main(["loop", "act", "leads"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(loops, "launch", lambda name, env: 77)
    assert cli.main(["loop", "start", "leads"]) == 0
    assert capsys.readouterr().out == "loop leads running (pid 77); `cauce loop show leads` says how it goes.\n"


# --- workflow_cli: how runs and definitions are said ----------------------------------------


def test_a_step_line_says_its_cell_tries_phase_cost_and_how_far_behind_it_is():
    assert wc._files([], 0) == ""
    step = {"state": "running", "task": 4, "cell": "sonnet/high", "tries": 2, "phase": "implementing",
            "cost_usd": 0.5, "cost_live": True, "behind": "2 commits behind main"}
    assert wc._step_line(step) == ("running #4 · at sonnet/high · try 2 · implementing · $0.50 so far · "
                                   "2 commits behind main")
    assert wc._step_line({"state": "running", "cost_running": True}) == "running · running"
    assert wc._money({"cost_usd": 1.5, "cost_running": True}) == "$1.50 + running"


def test_a_run_line_with_no_step_is_its_head_and_names_an_older_last_commit():
    empty = {"id": 3, "name": "w", "state": "done", "cost_usd": 0, "waiting": [], "steps": [], "points": 5}
    assert wc._run_line(empty) == "#3 w — done · $0.00 · 5 points"
    run = {"id": 3, "name": "w", "state": "running", "cost_usd": 0, "waiting": [], "steps": [
        {"id": "a", "state": "done", "commit": "abc1234 built it"},
        {"id": "b", "state": "running", "task": 9}]}
    assert wc._run_line(run) == "#3 w — running · $0.00 · step b (2/2): running #9 · last commit abc1234 built it"


def test_a_compact_step_says_its_tries_sub_run_answer_and_why_it_was_skipped():
    assert wc._compact_step({"state": "done", "task": 2, "tries": 3, "cost_usd": 0.25, "kept_branch": "cauce/task-2",
                             "subrun": 8}) == ("done #2 · try 3 · $0.25 · branch cauce/task-2 · runs run #8 "
                                               "(`cauce workflow status 8`)")
    assert wc._compact_step({"state": "done", "task": 5, "answer": "yes", "options": ["yes", "no"]}) == \
        "done #5 · $0.00 · answered yes"
    assert wc._compact_step({"state": "done", "skipped": "when check is findings: it was clean"}) == \
        "done · when check is findings: it was clean"


def test_a_run_in_full_says_each_part_s_detail(capsys):
    wc._print_run({"id": 1, "name": "w", "state": "running", "cost_usd": 0, "cwd": "/x", "inputs": {},
                   "waiting": [], "branch": None, "steps": [
                       {"id": "build", "state": "running", "parts": [
                           {"id": "web", "state": "done", "task": 3, "phase": "checking", "commit": "c1 web",
                            "files": ["a.py", "b.py", "c.py", "d.py"], "files_n": 5}]}]}, full=True)
    out = capsys.readouterr().out.splitlines()
    assert out[-2].strip().startswith("✓ web")
    assert out[-1].strip() == "checking · c1 web · 5 file(s): a.py, b.py, c.py +2"


def test_a_definition_says_its_question_and_sub_workflow_steps(capsys):
    defn = workflows.normalize({"name": "ask", "inputs": ["goal"], "steps": [
        {"id": "plan", "prompt": "Plan {goal}", "kind": "plan"},
        {"id": "go", "ask": "Build it?", "options": ["yes", "no"]},
        {"id": "sub", "workflow": "bugfix", "with": {"goal": "{goal}"}, "when": {"go": "yes"}},
        {"id": "bare", "workflow": "bugfix", "needs": ["sub"]}]})
    wc._print_definition({**defn, "scope": "user", "path": "/p"})
    out = capsys.readouterr().out
    assert "      asks the person: Build it?\n      answers: yes, no" in out
    assert "  sub — sub after go when go is yes\n      runs the workflow bugfix with goal={goal}" in out
    assert out.rstrip().endswith("      runs the workflow bugfix")


# --- workflow_cli: the commands ------------------------------------------------------------


def _wf(*args: str) -> int:
    return cli.main(["workflow", *args])


def test_the_cli_lists_a_broken_definition_shows_a_bundled_one_and_refuses_what_is_no_json(
        git_repo, monkeypatch, capsys, tmp_path):
    from cauce.store import home

    monkeypatch.chdir(git_repo)
    folder = home() / "workflows"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{")
    assert _wf("list") == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("broken"))
    assert "is not readable JSON" in line
    assert _wf("show", "bugfix") == 0
    out = capsys.readouterr().out
    assert out.startswith("bugfix v") and "(bundled: " in out and "in use by run" not in out
    assert "revision sha256:" in out
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert _wf("save", str(bad)) == 1
    assert capsys.readouterr().err.startswith("cauce workflow: not JSON: ")


def test_the_cli_applies_lists_and_says_no_pending_change(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    path = workflows.save(workflows.skeleton("ship"), git_repo).path
    assert _wf("pending") == 0 and capsys.readouterr().out == "no change waits for a run to end\n"
    waiting = workflows.pending_path(path)
    waiting.parent.mkdir()
    waiting.write_text(json.dumps({**workflows.skeleton("ship", "changed"), "version": 2}))
    assert _wf("pending", "--json") == 0
    (found,) = json.loads(capsys.readouterr().out)
    assert found["name"] == "ship" and found["held_by"] == [] and found["scope"] == "user"
    assert _wf("pending", "ship", "--apply") == 0
    assert capsys.readouterr().out == f"applied: {path}\n"
    assert json.loads(path.read_text())["description"] == "changed" and not waiting.exists()


def test_an_unknown_workflow_command_is_refused_and_a_step_set_with_nothing_changes_nothing(git_repo, monkeypatch,
                                                                                           capsys):
    assert wc.cmd_workflow(argparse.Namespace(workflow_command="bogus")) == 2
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    assert _wf("step", "set", "ship", "do") == 1
    assert "nothing to change" in capsys.readouterr().err
    assert _wf("step", "set", "ship", "do", "--surfaces", "src/, docs/*.md,") == 0
    assert workflows.load("ship", git_repo)["steps"][0]["surfaces"] == ["src/**", "docs/*.md"]  # a folder: all under it


def _editor(tmp_path, body: str) -> str:
    script = tmp_path / "editor.py"
    script.write_text("import json, sys\npath = sys.argv[1]\n" + body)
    return f"{sys.executable} {script}"


def test_an_edit_in_the_editor_is_kept_when_it_holds_and_said_when_it_does_not(git_repo, monkeypatch, capsys,
                                                                               tmp_path):
    monkeypatch.chdir(git_repo)
    path = workflows.save(workflows.skeleton("ship"), git_repo).path
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", _editor(tmp_path, "d = json.load(open(path))\nd['description'] = 'edited'\n"
                                                   "json.dump(d, open(path, 'w'))\n"))
    assert _wf("edit", "ship") == 0
    assert capsys.readouterr().out == f"saved in the user scope: {path}\n"
    assert json.loads(path.read_text())["description"] == "edited" and json.loads(path.read_text())["version"] == 2
    # A pending change is what the editor opens, and a bundled one is copied to the user scope.
    waiting = workflows.pending_path(path)
    waiting.parent.mkdir()
    waiting.write_text(json.dumps({**workflows.skeleton("ship", "waiting"), "version": 3}))
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "assert json.load(open(path))['description'] == 'waiting'\n"))
    assert _wf("edit", "ship") == 0
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "open(path, 'w').write('{broken')\n"))
    assert _wf("edit", "ship") == 1
    err = capsys.readouterr().err
    assert "not kept, nothing changed" in err and "(your edit is in " in err
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "pass\n"))
    assert _wf("edit", "bugfix") == 0
    assert "saved in the user scope" in capsys.readouterr().out
    assert workflows.load("bugfix", git_repo)["scope"] == "user"


def test_a_run_s_check_fills_its_inputs_without_a_text(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    workflows.save({"name": "checked", "inputs": ["goal", "cmd"], "steps": [
        {"id": "do", "prompt": "{goal}", "verify": "{cmd}"}]}, git_repo)
    args = argparse.Namespace(input=["goal=the export"], text=None, verify="pytest -q", name="checked", scope=None)
    assert wc._inputs(args, git_repo) == {"goal": "the export", "cmd": "pytest -q"}


def test_a_run_s_help_without_a_name_or_with_a_wrong_one_still_helps(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    parser = argparse.ArgumentParser(prog="run", add_help=False)
    parser.add_argument("-h", action=wc._RunHelp)
    with pytest.raises(SystemExit):
        parser.parse_args(["-h"])
    assert capsys.readouterr().out.startswith("usage: run")
    with pytest.raises(SystemExit):
        _wf("run", "Not-A-Name", "--help")
    out = capsys.readouterr().out
    assert "\nNot-A-Name: 'Not-A-Name' is no workflow name" in out


def test_a_run_of_many_features_is_told_so_and_a_dry_run_checks_the_run_it_waits_for(git_repo, monkeypatch,
                                                                                    capsys, kicks):
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    monkeypatch.setattr(workflows, "many_features", lambda goal: "this goal lists 7 items")
    assert _wf("run", "ship", "the export", "--dry-run", "--after", "999") == 1
    out, err = capsys.readouterr()
    assert "cauce: this goal lists 7 items" in out and "there is no workflow run #999" in err


def test_a_run_queued_after_another_says_what_it_waits_for(git_repo, monkeypatch, capsys, kicks, store: Store):
    monkeypatch.chdir(git_repo)
    workflows.save(workflows.skeleton("ship"), git_repo)
    assert _wf("run", "ship", "first") == 0
    first = store.list_runs()[0]["id"]
    capsys.readouterr()
    assert _wf("run", "ship", "second", "--after", str(first)) == 0
    out = capsys.readouterr().out
    assert f"\nit waits for run #{first} to pass\n" in out
    assert _wf("status", "--full") == 0
    out = capsys.readouterr().out
    # two runs in full, a blank line between them
    assert out.count("run #") >= 2 and "\n\nrun #" in out
