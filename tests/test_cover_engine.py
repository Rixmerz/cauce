"""Behaviour of the engine's less travelled branches: preflight, progress, stops, escalate, the
dispatcher, the launcher, worktrees and the orchestrator. Each test asserts what the code returns,
records or leaves behind; where a branch decides a verdict, the failing-closed side is asserted."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cauce import preflight

from .conftest import git

# --- preflight ---------------------------------------------------------------------------------


def _bin(folder: Path, *names: str, body: str = "exit 0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        tool = folder / name
        tool.write_text(f"#!/bin/sh\n{body}\n")
        tool.chmod(0o755)
    return folder


@pytest.fixture
def penv(tmp_path):
    """A PATH with only a shell and the tools a test adds, and an empty nvm."""
    tools = tmp_path / "path"
    tools.mkdir()
    for name in ("sh", "cat"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    (tmp_path / "nvm").mkdir()
    return {"PATH": str(tools), "HOME": str(tmp_path), "NVM_DIR": str(tmp_path / "nvm")}


@pytest.fixture
def proj(tmp_path):
    where = tmp_path / "project"
    where.mkdir()
    return where


def test_preflight_says_what_it_could_not_tell_once_each(proj, penv):
    result = preflight.check("$(which tool) run; $(which tool) run", proj, env=penv)
    assert result.ok
    lines = result.plan_lines()
    assert len(lines) == 1 and lines[0].startswith("preflight could not tell, and goes on: ")
    # the same unknown twice is said once
    assert lines[0].count("substitution") == 1


def test_preflight_reads_no_program_from_an_unbalanced_quote_or_bare_assignments(proj, penv):
    # an unbalanced quote cannot be split: no program read, no stop
    assert preflight.check("missingtool 'unterminated", proj, env=penv).ok
    # assignments alone start nothing
    result = preflight.check("FOO=1 BAR=2", proj, env=penv)
    assert result.ok and not result.unknown


def test_preflight_reads_the_program_after_a_subshells_bracket(proj, penv):
    result = preflight.check("( nosuchprog -q )", proj, env=penv)
    assert not result.ok and "`nosuchprog` is not on the PATH" in result.blockers[0].what


def test_preflight_points_at_npx_for_a_tool_only_node_modules_has(proj, penv):
    _bin(proj / "node_modules" / ".bin", "eslint")
    result = preflight.check("eslint .", proj, env=penv)
    assert not result.ok and result.blockers[0].fix.startswith("write `npx eslint`")


def test_preflight_a_missing_program_the_request_names_is_no_stop(proj, penv):
    result = preflight.check("mytool --check", proj, asked="add a mytool script", env=penv)
    assert result.ok and any("the request names it" in u for u in result.unknown)


def test_preflight_a_scripts_missing_tool_is_no_stop_while_the_worker_installs(proj, penv):
    (proj / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint ."}}))
    _bin(Path(penv["PATH"]), "npm")
    result = preflight.check("npm run lint", proj, env=penv, installing=True)
    assert result.ok and any("not installed yet" in u for u in result.unknown)
    # without the install and without dependencies that may provide it, it stops
    assert not preflight.check("npm run lint", proj, env=penv).ok


def test_preflight_an_npm_script_with_no_package_json_is_unknown(proj, penv):
    _bin(Path(penv["PATH"]), "npm")
    result = preflight.check("npm run build", proj, env=penv)
    assert result.ok and "no package.json here for `npm build`" in result.unknown


def test_preflight_an_unreadable_node_spec_and_a_silent_node_are_unknown(proj, penv):
    _bin(Path(penv["PATH"]), "npm")
    (proj / ".nvmrc").write_text("lts/*\n")
    result = preflight.check("npm --version", proj, env=penv)
    assert result.ok and any("no version cauce compares" in u for u in result.unknown)
    (proj / ".nvmrc").write_text("22\n")
    # no node on the PATH at all: it did not answer
    result = preflight.check("npm --version", proj, env=penv)
    assert result.ok and "the PATH's Node did not answer" in result.unknown


def test_preflight_a_node_that_fits_is_no_stop(proj, penv):
    _bin(Path(penv["PATH"]), "npm")
    _bin(Path(penv["PATH"]), "node", body="echo v22.3.0")
    (proj / ".nvmrc").write_text("22\n")
    assert preflight.check("npm --version", proj, env=penv).ok
    (proj / ".nvmrc").write_text("24\n")
    result = preflight.check("npm --version", proj, env=penv)
    assert not result.ok and result.blockers[0].fix == "`nvm install 24`"


def _venv_python(proj: Path, script: str) -> None:
    folder = proj / ".venv" / "bin"
    folder.mkdir(parents=True)
    python = folder / "python"
    python.write_text(f"#!/bin/sh\n{script}\n")
    python.chmod(0o755)


def test_preflight_python_package_reads_nothing_it_cannot(proj, penv):
    _bin(Path(penv["PATH"]), "pytest")
    _venv_python(proj, "echo /elsewhere/pkg/__init__.py")
    # no pyproject: nothing said
    assert preflight.check("pytest", proj, env=penv).warnings == []
    (proj / "pyproject.toml").write_text("not = [toml")
    assert preflight.check("pytest", proj, env=penv).warnings == []
    # a project named, but no package folder for it here
    (proj / "pyproject.toml").write_text('[project]\nname = "my-pkg"\n')
    assert preflight.check("pytest", proj, env=penv).warnings == []
    # with its folder, an import from elsewhere is a warning, never a stop
    (proj / "src" / "my_pkg").mkdir(parents=True)
    result = preflight.check("pytest", proj, env=penv)
    assert result.ok and "imports `my_pkg` from /elsewhere/pkg/__init__.py" in result.warnings[0]


def test_preflight_python_package_a_probe_that_fails_or_breaks(proj, penv, monkeypatch):
    _bin(Path(penv["PATH"]), "pytest")
    (proj / "pyproject.toml").write_text('[project]\nname = "pkg"\n')
    (proj / "pkg").mkdir()
    _venv_python(proj, "echo /elsewhere; exit 1")
    # a probe that exits non-zero says nothing
    assert preflight.check("pytest", proj, env=penv).warnings == []

    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired("python", 10)

    monkeypatch.setattr(preflight.subprocess, "run", broken)
    result = preflight.check("pytest", proj, env=penv)
    assert result.ok and "where `.venv` imports `pkg` from" in result.unknown


def test_preflight_that_breaks_fails_open(proj, penv, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(preflight, "_scripts", boom)
    result = preflight.check("nosuchprog", proj, env=penv)
    # the blocker it found before it broke is cleared: a preflight that breaks cannot tell
    assert result.ok and result.unknown[-1] == "the preflight itself failed (RuntimeError)"


# --- progress ----------------------------------------------------------------------------------


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _worktree_for(repo: Path, task_id: int) -> Path:
    from cauce.config import home

    path = home() / "worktrees" / f"task-{task_id}"
    git(repo, "worktree", "add", "-q", "-b", f"cauce/task-{task_id}", str(path))
    return path


def test_progress_estimate_and_expected_line_need_a_kind_and_passes(store):
    from cauce import progress

    assert progress.estimate(store, {"id": 1, "repo": "r"}) is None
    # options that do not parse fall back to the rules; with no passes there is nothing to say
    assert progress.expected_line(store, {"id": 1, "body": "fix the parser", "options": "{nope"}) == ""
    assert progress.expected_line(store, {"id": 1, "body": "x", "options": json.dumps({"kind": "implement"})}) == ""


def test_progress_new_lines_of_a_file_it_cannot_read_are_none(tmp_path):
    from cauce import progress

    assert progress._new_lines(tmp_path / "gone.py") == 0


def test_progress_changes_leave_artifacts_out_and_say_none_when_git_cannot(git_repo, store):
    from cauce import progress
    from cauce.config import home

    (git_repo / "__pycache__").mkdir()
    (git_repo / "__pycache__" / "x.pyc").write_text("a\n")
    git(git_repo, "add", "-f", "-A")
    git(git_repo, "commit", "-q", "-m", "artifact")
    base = _head(git_repo)
    path = _worktree_for(git_repo, 7)
    (path / "__pycache__" / "x.pyc").write_text("b\nc\n")  # a tracked artifact changed: no change
    (path / "app.py").write_text("print('hi')\nprint('more')\n")
    assert progress.changes(7, base) == {"files": 1, "added": 1, "removed": 0, "paths": ["app.py"]}
    # a worktree folder git cannot read
    (home() / "worktrees" / "task-8").mkdir(parents=True)
    assert progress.changes(8, base) is None
    assert progress.peek(8, base) is None


def test_progress_changes_in_place_count_only_what_moved(git_repo):
    from cauce import isolate, progress

    (git_repo / "keep.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "keep")
    (git_repo / "keep.py").write_text("x = 2\n")  # the person's own change, before the attempt
    places = isolate.stand(git_repo, [git_repo])
    (git_repo / "app.py").write_text("print('changed')\n")
    found = progress.changes(999, None, places=places)
    # keep.py differs from HEAD but did not move since the attempt started: not the worker's
    assert found == {"files": 1, "added": 1, "removed": 1, "paths": ["app.py"]}


def test_progress_peek_in_place_skips_still_checkouts_and_says_none_when_git_fails(git_repo, tmp_path):
    from cauce import isolate, progress

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "o.txt").write_text("o\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "o")
    places = isolate.stand(tmp_path, [git_repo, other])
    (git_repo / "app.py").write_text("print('changed')\n")
    found = progress.peek(999, None, places=places)
    # the checkout that did not move is not listed
    assert [p["path"] for p in found["places"]] == [str(git_repo)] and "changed" in found["diff"]
    # a head git cannot read: git cannot say
    broken = [{**places[0], "head": "0" * 40}]
    assert progress.peek(999, None, places=broken) is None


def test_progress_peek_goes_on_past_a_new_file_git_cannot_diff(git_repo, store, monkeypatch):
    from cauce import isolate, progress

    places = isolate.stand(git_repo, [git_repo])
    (git_repo / "new.py").write_text("n = 1\n")
    path = _worktree_for(git_repo, 9)
    (path / "fresh.py").write_text("f = 1\n")
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if "--no-index" in argv:
            raise OSError("no")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(progress.subprocess, "run", run)
    in_place = progress.peek(999, None, places=places)
    assert in_place is not None and "n = 1" not in in_place["diff"]
    in_tree = progress.peek(9, _head(git_repo))
    assert in_tree is not None and "f = 1" not in in_tree["diff"] and in_tree["cut"] is False


def test_progress_texts_none_without_a_checkout_and_a_deleted_file_has_no_new(git_repo, tmp_path):
    from cauce import progress

    assert progress.texts(tmp_path / "nowhere", None) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert progress.texts(plain, None) is None
    (git_repo / "app.py").unlink()
    found = progress.texts(git_repo, None)
    assert found == {"files": [{"path": "app.py", "old": "print('hi')\n", "new": None}], "more": 0}


def test_progress_of_without_a_plan_carries_the_estimate(store):
    from cauce import progress

    from .test_standards import finished

    for wall in (60, 120, 180):
        finished(store, ("sonnet/medium", True), wall=wall)
    task = store.create_task("x", status="running", source="queue", repo="r", kind="implement", points=3)
    found = progress.of(store, task)
    assert "elapsed_s" not in found and found["estimate"]["p50_s"] == 120 and found["stage"] == "worker"


# --- escalate ----------------------------------------------------------------------------------


def test_escalate_streaks_stop_at_the_first_try_that_breaks_them():
    from cauce.escalate import Attempt, Failure, load_streak, reshape_streak
    from cauce.matrix import Cell

    cell, other = Cell("sonnet", "medium"), Cell("sonnet", "high")
    plain = Attempt(cell, 30, False, Failure.CODE_BUG)
    loaded = Attempt(cell, 30, False, Failure.ENVIRONMENT, load="timeouts")
    assert load_streak([loaded, plain, loaded, loaded]) == 2
    shaped = Attempt(cell, 30, False, Failure.INCONCLUSIVE, shape="two blocks")
    # a reshape on another cell is not this cell's streak
    assert reshape_streak([Attempt(other, 30, False, Failure.INCONCLUSIVE, shape="x"), shaped]) == 1


def test_escalate_skipped_names_nothing_for_a_target_off_the_ladder():
    from cauce import escalate
    from cauce.matrix import LADDERS, Cell

    assert escalate._skipped(LADDERS["implement"], Cell("sonnet", "medium"), Cell("haiku", "low")) == ()


# --- stops -------------------------------------------------------------------------------------


def test_stops_supersede_leaves_other_text_alone(store):
    from cauce import stops

    passed = store.create_task("add the export", status="done", source="cauce", repo="r")
    copy = store.create_task("add  the export", status="failed", source="cauce", repo="r")
    other = store.create_task("add the import", status="failed", source="cauce", repo="r")
    assert stops.supersede(store, passed) == [copy["id"]]
    assert store.get_task(other["id"])["status"] == "failed"
    assert stops.of(store, store.get_task(copy["id"])).cause == "superseded"
    # a pass with no repository, or no text, supersedes nothing
    loose = store.create_task("add the import", status="done", source="cauce")
    assert stops.supersede(store, loose) == [] and store.get_task(other["id"])["status"] == "failed"
    assert stops.supersede(store, {**passed, "body": "  "}) == []


def test_stops_landed_reads_only_tasks_of_this_checkout(store, git_repo, tmp_path):
    from cauce import stops

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    git(elsewhere, "init", "-q", "-b", "main")
    task = store.create_task("add the export", status="running", source="cauce", cwd=str(elsewhere), repo="r")
    stops.record(store, task["id"], stops.Stop("died", "its process went away"))
    git(git_repo, "checkout", "-q", "-b", f"cauce/task-{task['id']}")
    (git_repo / "export.py").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "export")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", f"cauce/task-{task['id']}")
    # its branch landed here, but the task was sent from another checkout: not this one's to sweep
    assert stops.landed(store, git_repo) == []
    assert store.get_task(task["id"])["status"] == "interrupted"


def test_stops_retried_leaves_live_and_passed_work_alone(store):
    from cauce import stops

    for status in ("queued", "running", "done"):
        task = store.create_task(f"step {status}", status=status, source="queue", repo="r")
        stops.retried(store, task, 99)
        assert store.get_task(task["id"])["status"] == status
        assert store.final_event(task["id"]) is None


# --- dispatch ----------------------------------------------------------------------------------


def test_dispatch_named_surfaces_leave_globs_out():
    from cauce import dispatch

    task = {"body": "change src/*.py and src/app/main.py, then src/app/main.py again"}
    assert dispatch.named_surfaces(task) == ("src/app/main.py",)


def test_dispatch_a_usage_limit_kept_in_a_form_it_cannot_read_holds_nothing(store):
    from cauce import dispatch, stops

    store.set_meta(stops.LIMIT_META, "tomorrow")
    assert dispatch._limit_holds(store) is None


def test_dispatch_a_waiter_that_cannot_start_records_no_wait(store, tmp_path):
    from cauce import dispatch

    task = store.create_task("x", status="blocked", source="cauce", cwd=str(tmp_path))

    def fails(*args, **kwargs):
        raise OSError("no process")

    dispatch._wake_at(store, task["id"], 1e10, tmp_path, {}, fails)
    assert store.events_of(task["id"], "limit_waiting") == []


def test_dispatch_alive_reads_a_pid_it_cannot_signal_as_alive():
    from cauce import dispatch

    assert dispatch._alive(None) is False
    # a pid no kernel holds cannot be told dead: kept as alive, so nothing is swept on a guess
    assert dispatch._alive(2 ** 70) is True


def test_dispatch_claim_skips_a_resume_whose_time_it_cannot_read(store):
    from cauce import dispatch, stops

    task = store.create_task("x", status="blocked", source="cauce")
    stop = stops.Stop("died", "gone")
    store.add_event(task["id"], "auto_resumed", after="gone", cause="died")
    store._conn.execute("UPDATE events SET ts = 'garbage' WHERE task_id = ? AND kind = 'auto_resumed'",
                        (task["id"],))
    assert dispatch._claim_resume(store, task["id"], stop, 0) is True
    assert len(store.events_of(task["id"], "auto_resumed")) == 2


def test_dispatch_start_resume_without_a_store_keeps_no_log_offset(store, tmp_path):
    from cauce import dispatch, stops

    started = []
    task = {"id": 5, "cwd": str(tmp_path)}
    assert dispatch._start_resume(task, stops.Stop("turns", "out of turns"), tmp_path, {},
                                  lambda argv, **kw: started.append(argv)) is True
    assert started[0][3:] == ["resume", "5", "--unattended", "--max-turns", str(stops.RESUME_TURNS)]
    assert store.meta("log_from:5") is None


def test_dispatch_recover_skips_an_ending_whose_time_it_cannot_read(store, tmp_path, monkeypatch):
    from cauce import dispatch, stops

    monkeypatch.setenv("CAUCE_AUTOWORK", "on")
    task = store.create_task("x", status="running", source="cauce", cwd=str(tmp_path))
    stops.record(store, task["id"], stops.Stop("died", "gone"))
    store._conn.execute("UPDATE events SET ts = 'garbage' WHERE task_id = ? AND kind = 'finished'", (task["id"],))
    assert dispatch.recover(store, tmp_path, {"CAUCE_AUTOWORK": "on"}, popen=pytest.fail) == []
    assert store.events_of(task["id"], "auto_resumed") == []


# --- flow --------------------------------------------------------------------------------------


def _slow_task(store, repo="r"):
    task = store.create_task("build it", status="running", source="queue", repo=repo, kind="implement")
    store.add_event(task["id"], "planned", kind="implement")
    return store.get_task(task["id"])


def test_flow_slow_reads_a_kept_history_and_says_nothing_inside_it(store):
    import time as clock

    from cauce import flow

    task = _slow_task(store)
    late = clock.time() + flow.SLOW_MIN_S + 600
    seen = {("r", "implement"): [60.0] * flow.SLOW_SAMPLES}
    said = flow.slow(store, task, now=late, seen=seen)
    assert said is not None and f"of the {flow.SLOW_SAMPLES} implement tasks" in said
    # inside 9 in 10 of what passed here: nothing said
    assert flow.slow(store, task, now=late, seen={("r", "implement"): [1e9] * flow.SLOW_SAMPLES}) is None
    # a caller's cache is filled from the store when it holds nothing for the key
    cache: dict = {}
    assert flow.slow(store, task, now=late, seen=cache) is None and cache == {("r", "implement"): []}


def test_flow_slow_says_nothing_of_a_plan_whose_time_it_cannot_read(store):
    import time as clock

    from cauce import flow

    task = _slow_task(store)
    store._conn.execute("UPDATE events SET ts = 'garbage' WHERE task_id = ? AND kind = 'planned'", (task["id"],))
    seen = {("r", "implement"): [60.0] * flow.SLOW_SAMPLES}
    assert flow.slow(store, task, now=clock.time() + 1e6, seen=seen) is None


def test_flow_alive_reads_a_process_it_may_not_signal_as_alive(monkeypatch):
    from cauce import flow

    def refuse(pid, sig):
        raise PermissionError

    monkeypatch.setattr(flow.os, "kill", refuse)
    assert flow.alive(1234) is True


def test_flow_prune_reads_past_tasks_whose_directory_is_gone(store, tmp_path, monkeypatch):
    from cauce import flow

    monkeypatch.setenv("CAUCE_PRUNE", "on")
    store.create_task("x", status="done", source="cauce", cwd=str(tmp_path / "gone"), repo="r")
    store.create_task("y", status="done", source="cauce", repo="r")
    assert flow.prune(store) == []


def test_flow_judged_keeps_a_readers_or_a_lone_writers_earlier_yes(store):
    from cauce import flow

    reader = store.enqueue("read the docs and explain", repo="r", cwd="/", options={"kind": "explore"})
    store.set_parallel(reader["id"], True, "earlier")
    reader = store.get_task(reader["id"])
    out = flow._judged(store, reader, [], pytest.fail, pytest.fail)
    assert out["parallel"] == 1 and store.get_task(reader["id"])["parallel_reason"] == "earlier"
    writer = store.enqueue("add the export", repo="r", cwd="/", options={"kind": "implement"})
    store.set_parallel(writer["id"], True, "earlier")
    writer = store.get_task(writer["id"])
    out = flow._judged(store, writer, [reader], pytest.fail, pytest.fail)
    # the model was never asked: `decide` is `pytest.fail`
    assert out["parallel"] == 1 and store.get_task(writer["id"])["parallel_reason"] == "earlier"


def test_flow_a_wait_whose_decision_time_cannot_be_read_is_not_rejudged(store):
    from cauce import flow

    task = store.enqueue("add the export", repo="r", cwd="/")
    store.set_parallel(task["id"], False, "may conflict")
    store.add_event(task["id"], "dispatch_decided", parallel=False, by="haiku")
    store._conn.execute("UPDATE events SET ts = 'garbage' WHERE task_id = ? AND kind = 'dispatch_decided'",
                        (task["id"],))
    assert flow._may_have_moved(store, store.get_task(task["id"])) is False


def test_flow_limited_until_ignores_a_reset_it_cannot_read(store):
    from cauce import flow, stops

    store.set_meta(stops.LIMIT_META, "soon")
    assert flow.limited_until(store) is None


def test_flow_a_process_whose_task_is_gone_ended_failed(store):
    from cauce import flow

    handle = object.__new__(flow._Process)
    handle.task_id, handle.store = 98765, store
    handle.proc = subprocess.Popen([sys.executable, "-c", "pass"])
    handle.proc.wait()
    assert handle.poll() == "failed"


def test_flow_the_last_crash_is_the_last_deaths_not_a_later_stop(store):
    from cauce import flow, stops

    task = store.create_task("x", status="running", source="cauce")
    stops.record(store, task["id"], stops.Stop("died", "gone", extra={"crash": "ValueError: bad"}))
    stops.record(store, task["id"], stops.Stop("turns", "out of turns"))
    assert flow._last_crash(store, task["id"]) == "ValueError: bad"


def test_flow_settle_keeps_its_error_and_never_stops_the_queue(store, monkeypatch):
    from cauce import evolution, flow

    def boom(store):
        raise RuntimeError("calibration broke")

    monkeypatch.setattr(evolution, "settle", boom)
    flow._settle(store)
    assert store.events_of(0, "evolution_error")[0]["data"]["error"] == "RuntimeError: calibration broke"


def test_flow_work_waits_out_a_full_disk_at_its_first_sweep_and_raises_its_own_errors(store, monkeypatch):
    import errno

    from cauce import flow

    def full(store, **kw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(flow, "sweep", full)
    report = flow.work(store, pause=lambda: None, backoff=lambda s: None)
    assert report.ran == []

    def broken(store, **kw):
        raise RuntimeError("cauce's own")

    monkeypatch.setattr(flow, "sweep", broken)
    with pytest.raises(RuntimeError, match="cauce's own"):
        flow.work(store, pause=lambda: None, backoff=lambda s: None)


def test_flow_work_starts_nothing_another_dispatcher_claimed(store, monkeypatch):
    from cauce import flow

    task = store.enqueue("read the docs", repo="r", cwd="/")
    monkeypatch.setattr(store, "claim", lambda task_id: False)
    report = flow.work(store, start=pytest.fail, pause=lambda: None, backoff=lambda s: None)
    assert report.ran == [] and store.get_task(task["id"])["status"] == "queued"
    assert store.events_of(task["id"], "dispatched") == []


def test_flow_work_raises_a_start_that_failed_for_another_reason_than_space(store):
    from cauce import flow

    task = store.enqueue("read the docs", repo="r", cwd="/")

    def start(task, where):
        raise RuntimeError("spawn broke")

    with pytest.raises(RuntimeError, match="spawn broke"):
        flow.work(store, start=start, pause=lambda: None, backoff=lambda s: None)
    # not put back on the queue: only a full disk sends its work back
    assert store.get_task(task["id"])["status"] != "queued"


# --- launch ------------------------------------------------------------------------------------


def test_launch_resets_in_reads_a_dated_reset_and_refuses_a_date_that_is_none():
    from datetime import UTC, datetime

    from cauce import launch

    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert launch.resets_in("limit reached, resets Feb 30, 9am (UTC)", now=now) is None
    # a date already past this year is next year's
    left = launch.resets_in("limit reached, resets Jan 2, 9am (UTC)", now=now)
    assert left == (datetime(2027, 1, 2, 9, 0, tzinfo=UTC) - now).total_seconds()


def test_launch_cli_help_of_a_cli_that_cannot_run_is_empty(tmp_path):
    from cauce import launch

    launch._cli_help.cache_clear()
    assert launch._cli_help(str(tmp_path / "no-claude")) == ""
    assert launch.lean_flags(str(tmp_path / "no-claude"), {"CAUCE_LEAN": "on"}) == ()


def test_launch_the_hosts_session_is_never_kept_as_the_workers(tmp_path, monkeypatch):
    from cauce import launch
    from cauce.matrix import Cell

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-1")
    out = json.dumps({"is_error": False, "session_id": "host-1", "num_turns": 1, "total_cost_usd": 0.0,
                      "result": "```cauce-result\n" + json.dumps({"verdict": "pass", "summary": "s",
                                                                  "evidence": "1 passed"}) + "\n```"})
    spec = launch.LaunchSpec(prompt="p", cell=Cell("sonnet", "high"), launch_dir=tmp_path)
    result = launch.parse(subprocess.CompletedProcess(["claude"], 0, out, ""), spec)
    assert result.session_id == ""


def test_launch_a_refusal_that_is_no_write_is_kept(tmp_path):
    from cauce import launch

    envelope = {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "npm run build"}},
                                       {"tool_name": "Write", "tool_input": {"file_path": "a.py"}}]}
    kept, dropped = launch._overcome(envelope, ("a.py",), tmp_path)
    assert dropped == ("Write(a.py)",)
    assert kept["permission_denials"] == [envelope["permission_denials"][0]]


def test_launch_parts_whose_surfaces_are_no_list_are_refused():
    from cauce import launch

    parts, _contract, problem = launch.read_parts({"parts": [{"id": "a", "prompt": "do a", "surfaces": "src/a"}]})
    assert parts == () and problem == "part 'a': its surfaces are a list of paths or globs"


def test_launch_answered_before_notices_skips_what_it_cannot_read(tmp_path, monkeypatch):
    from cauce import launch, usage

    folder = tmp_path / "cfg" / "projects" / "w"
    folder.mkdir(parents=True)
    block = "```cauce-result\n" + json.dumps({"verdict": "pass", "summary": "s"}) + "\n```"
    lines = ["not json", json.dumps({"type": "system", "message": {}}),
             json.dumps({"type": "user", "message": {"content": "the task"}}),
             json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": block}]}})]
    (folder / "s2.jsonl").write_text("\n".join(lines) + "\n")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    assert launch.result_block(launch.answered_before_notices("s2", env))["verdict"] == "pass"
    # a transcript that cannot be read says nothing
    monkeypatch.setattr(usage, "transcript_of", lambda session, env=None: tmp_path)
    assert launch.answered_before_notices("s2", env) is None


def test_launch_tracked_state_of_a_folder_that_is_gone_or_holds_broken_checkouts(tmp_path, monkeypatch):
    from cauce import launch

    assert launch.tracked_state(tmp_path / "gone") is None
    folder = tmp_path / "folder"
    for name in ("a", "b", "c"):
        (folder / name).mkdir(parents=True)
        git(folder / name, "init", "-q", "-b", "main")
    (folder / "a" / ".git").rename(folder / "a" / "git-gone")
    (folder / "a" / ".git").write_text("gitdir: /nowhere\n")  # a checkout git cannot read
    state = launch.tracked_state(folder)
    assert not any(key.startswith("a/") for key in state) and "\0HEAD:b" in state
    monkeypatch.setattr(launch, "NESTED_MAX", 1)
    assert launch.nested_checkouts(folder) == [folder / "a"]


# --- isolate -----------------------------------------------------------------------------------


def _commit_in(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", name)
    return git(path, "rev-parse", "HEAD")


def test_isolate_a_mended_branch_goes_back_past_every_commit_it_cannot_read(git_repo, tmp_path):
    from cauce import isolate

    ws = isolate.prepare(git_repo, 21, tmp_path / "home")
    good = _commit_in(ws.path, "a.py", "a\n")
    lost = [_commit_in(ws.path, "b.py", "b\n"), _commit_in(ws.path, "c.py", "c\n")]
    git(git_repo, "worktree", "remove", "--force", str(ws.path))
    for sha in lost:
        (git_repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    again = isolate.prepare(git_repo, 21, tmp_path / "home")
    assert git(git_repo, "rev-parse", "refs/heads/cauce/task-21") == good
    assert (again.path / "a.py").exists() and not (again.path / "b.py").exists()


def test_isolate_links_carry_and_drop_aside_touch_only_what_is_theirs(git_repo, tmp_path):
    from cauce import isolate

    ws = isolate.prepare(git_repo, 22, tmp_path / "home")
    ws = isolate.Workspace(ws.repo_dir, ws.path, ws.branch, ws.base, links=("deps/missing", "deps/dir"))
    (ws.path / "deps" / "dir").mkdir(parents=True)
    isolate.unlink(ws)  # a folder of the worker's own is no link: left, with its parent
    assert (ws.path / "deps" / "dir").is_dir()
    # carry: a file the person deleted is deleted; one neither side has is skipped
    (ws.path / "gone.py").write_text("x\n")
    assert isolate.carry(ws, ["gone.py", "nowhere.py"]) == ["gone.py"]
    assert not (ws.path / "gone.py").exists()
    # drop_aside leaves another task's set-aside work alone
    (ws.path / "w.py").write_text("w\n")
    mine = isolate.set_aside(ws, 1)
    git(git_repo, "update-ref", "refs/cauce/aside/task-99-1", git(git_repo, "rev-parse", "HEAD"))
    isolate.drop_aside(ws)
    refs = git(git_repo, "for-each-ref", "--format=%(refname)", "refs/cauce/aside").split()
    assert mine not in refs and "refs/cauce/aside/task-99-1" in refs


def test_isolate_named_in_and_ignored_named_read_only_what_the_text_names(git_repo, tmp_path):
    from cauce import isolate

    assert isolate.named_in("fix the parser in app.py", ["app.py", "other.py"]) == ["app.py"]
    (git_repo / ".gitignore").write_text("secret.json\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "ignore")
    (git_repo / "secret.json").write_text("{}\n")
    (git_repo / "notes.txt").write_text("n\n")  # there, not ignored: no copy
    ws = isolate.prepare(git_repo, 23, tmp_path / "home")
    assert isolate.ignored_named(ws, "read secret.json and notes.txt") == ["secret.json"]
    # a file is copied, not linked
    assert isolate.bring(ws, ["secret.json"]) == (["secret.json"], [])
    assert (ws.path / "secret.json").is_file() and not (ws.path / "secret.json").is_symlink()


def test_isolate_a_race_lane_left_behind_is_replaced(git_repo, tmp_path):
    from cauce import isolate

    ws = isolate.prepare(git_repo, 24, tmp_path / "home")
    first = isolate.race_lane(ws, tmp_path / "home")
    (first.path / "stale.py").write_text("old\n")
    second = isolate.race_lane(ws, tmp_path / "home")
    assert second.path == first.path and not (second.path / "stale.py").exists()


def test_isolate_adopt_leaves_links_out_and_refuses_a_patch_that_does_not_apply(git_repo, tmp_path):
    from cauce import isolate

    deps = git_repo / "node_modules"
    deps.mkdir()
    (deps / "x.js").write_text("x\n")
    ws = isolate.prepare(git_repo, 25, tmp_path / "home")
    lane = isolate.race_lane(ws, tmp_path / "home")
    lane = isolate.Workspace(lane.repo_dir, lane.path, lane.branch, lane.base, links=("node_modules",))
    isolate.link(lane)
    (lane.path / "app.py").write_text("print('raced')\n")
    assert isolate.adopt(lane, ws) == ("app.py",)
    assert not (ws.path / "node_modules").exists()  # the link is not carried
    # a patch that cannot apply on the task's worktree is refused in a line
    lane2 = isolate.Workspace(lane.repo_dir, lane.path, lane.branch, git(git_repo, "rev-parse", "HEAD"),
                              links=())
    (lane.path / "app.py").write_text("print('again')\n")
    # the task's base holds another app.py than the one the patch was made against
    _commit_in(ws.path, "app.py", "something else entirely\n")
    bad_base = isolate.Workspace(ws.repo_dir, ws.path, ws.branch, git(ws.path, "rev-parse", "HEAD"))
    with pytest.raises(isolate.IsolationError, match="could not be carried over"):
        isolate.adopt(lane2, bad_base)


def test_isolate_changed_since_put_back_and_fingerprint_say_none_when_git_cannot(git_repo, tmp_path, monkeypatch):
    from cauce import isolate

    plain = tmp_path / "plain"
    plain.mkdir()
    assert isolate.changed_since(git_repo, "0" * 40) is None
    assert isolate.fingerprint(plain) is None
    snap = isolate.snapshot(git_repo)
    # a path the snapshot did not hold and the worktree does not have: nothing to do
    assert isolate.put_back(git_repo, snap, ["never.py"]) == []
    (git_repo / "app.py").unlink()
    (git_repo / "app.py").mkdir()  # a folder where the file must go back: cannot be written
    assert isolate.put_back(git_repo, snap, ["app.py"]) == ["app.py"]


def test_isolate_fingerprint_reads_renames_and_fails_closed_when_hashing_fails(git_repo, monkeypatch):
    from cauce import isolate

    git(git_repo, "mv", "app.py", "main.py")
    found = isolate.fingerprint(git_repo)
    assert set(found) == {"main.py"}  # the old name of a rename is skipped
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if "hash-object" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "broken")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(isolate.subprocess, "run", run)
    assert isolate.fingerprint(git_repo) is None
    assert isolate.rewritten(None, {"a": "1"}) == ()


def test_isolate_stand_leaves_out_what_git_cannot_read(git_repo, tmp_path, monkeypatch):
    from cauce import isolate

    plain = tmp_path / "plain"
    plain.mkdir()
    assert isolate.stand(tmp_path, [plain]) == []
    real = isolate._git

    def broken(cwd, *args, **kwargs):
        if Path(cwd) == tmp_path / "gone":
            raise OSError("gone")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(isolate, "_git", broken)
    assert [p["path"] for p in isolate.stand(tmp_path, [tmp_path / "gone", git_repo])] == [str(git_repo)]
    # a checkout that can no longer be read says nothing of what moved
    assert isolate.moved_in_place({"path": str(plain), "head": "HEAD", "dirty": {}}) is None


def test_isolate_undo_removes_only_files_the_base_lacked(git_repo, tmp_path):
    from cauce import isolate

    ws = isolate.prepare(git_repo, 26, tmp_path / "home")
    (ws.path / "new.py").write_text("n\n")
    isolate.undo(ws, ["new.py", "never.py"])
    assert not (ws.path / "new.py").exists() and not (ws.path / "never.py").exists()


def test_isolate_keep_left_needs_a_checkout_and_something_to_keep(git_repo, tmp_path):
    from cauce import isolate

    plain = tmp_path / "plain"
    plain.mkdir()
    assert isolate.keep_left(plain, 27, tmp_path / "home", message="m") is None
    ws = isolate.prepare(git_repo, 27, tmp_path / "home")
    assert isolate.keep_left(git_repo, 27, tmp_path / "home", message="m") is None
    assert ws.path.is_dir()  # nothing to keep: the worktree is left as it is


def test_isolate_numstat_generated_behind_say_nothing_when_git_cannot(git_repo, tmp_path):
    from cauce import isolate

    head = git(git_repo, "rev-parse", "HEAD")
    assert isolate.numstat(git_repo, "nope", head) is None
    assert isolate.generated(git_repo, []) == set()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert isolate.generated(plain, ["a.py"]) == set()
    assert isolate.behind(git_repo, head, "no-such-branch") is None
    assert isolate.behind_text({"base": "main", "commits": 1, "both": []}) == "behind main by 1 commit"


def test_isolate_behind_says_none_when_git_cannot_count(git_repo, monkeypatch):
    from cauce import isolate

    head = git(git_repo, "rev-parse", "HEAD")
    real = isolate._git

    def fake(cwd, *args, **kwargs):
        if args[:2] == ("rev-list", "--count"):
            return ""
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(isolate, "_git", fake)
    assert isolate.behind(git_repo, head, "main") is None


def test_isolate_landed_restore_prunable_on_branches_that_are_or_are_not_there(git_repo):
    from cauce import isolate

    head = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "cauce/task-30")
    _commit_in(git_repo, "x.py", "x\n")
    git(git_repo, "checkout", "-q", "main")
    # not in HEAD's history, and no paths to compare: it has not landed
    assert isolate.landed(git_repo, "cauce/task-30") is False
    # a branch that exists is not restored; a tip that is no commit is not either
    assert isolate.restore(git_repo, "cauce/task-30", head) is False
    assert isolate.restore(git_repo, "cauce/task-31", "0" * 40) is False
    assert isolate.prunable(git_repo, "cauce/task-404") is None
    # an orphan branch shares no history with HEAD: nothing says it landed
    git(git_repo, "checkout", "-q", "--orphan", "cauce/task-32")
    _commit_in(git_repo, "o.py", "o\n")
    git(git_repo, "checkout", "-q", "main")
    assert isolate.prunable(git_repo, "cauce/task-32") is None


def test_isolate_in_place_says_once_who_holds_the_checkout(git_repo, tmp_path, monkeypatch):
    import fcntl

    from cauce import isolate

    root = tmp_path / "home"
    lock = isolate.checkout_lock(root, git_repo)
    lock.parent.mkdir(parents=True)
    held = open(lock, "a+")  # noqa: SIM115
    held.write("41")
    held.flush()
    fcntl.flock(held, fcntl.LOCK_EX)
    monkeypatch.setattr(isolate, "WAIT_POLL_S", 0)
    said, checks = [], []

    def check():
        checks.append(1)
        if len(checks) == 3:
            fcntl.flock(held, fcntl.LOCK_UN)

    with isolate.in_place(root, git_repo, 42, waiting=said.append, check=check):
        assert lock.read_text() == "42"
    held.close()
    assert said == ["task #41"] and len(checks) == 3
