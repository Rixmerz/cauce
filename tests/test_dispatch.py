from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cauce import cli, dispatch, flow, hooks, launch
from cauce.store import Store

from .test_classify import _reply, _runner

RUNNING = {"id": 1, "status": "running", "body": "rewrite the importer"}
NEW = {"id": 2, "status": "queued", "body": "add a --dry-run flag to the importer"}


def test_nothing_ahead_needs_no_model():
    run = _runner(raises=AssertionError("no call"))
    assert dispatch.decide(NEW, [], runner=run) == dispatch.Decision(True, "nothing runs or waits ahead of it")
    assert run.calls == []


def test_haiku_reads_the_new_task_and_everything_ahead_of_it():
    run = _runner(_reply(parallel=False, reason="it changes the importer #1 rewrites"))
    got = dispatch.decide(NEW, [RUNNING], runner=run)
    assert got == dispatch.Decision(False, "it changes the importer #1 rewrites", 0.001)
    argv, kwargs = run.calls[0]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == dispatch.SCHEMA
    assert "#2" in kwargs["input"] and "- #1 (running): rewrite the importer" in kwargs["input"]
    long = {**NEW, "body": "x " * 2000}
    assert len(dispatch.question(long, [])) < dispatch.BODY_CHARS + 100


def test_any_failure_means_waiting_never_a_guess():
    for run in (_runner(raises=OSError("no claude")), _runner("not json"), _runner(_reply(parallel="yes", reason=""))):
        got = dispatch.decide(NEW, [RUNNING], runner=run)
        assert got.parallel is False and got.reason.startswith("waits its turn")
    blank = dispatch.decide(NEW, [RUNNING], runner=_runner(_reply(parallel=True, reason="  ")))
    assert blank == dispatch.Decision(True, "independent", 0.001)


def test_one_dispatcher_per_repository(tmp_path):
    with dispatch.hold(tmp_path, "r") as mine:
        assert mine and dispatch.held(tmp_path, "r") and not dispatch.held(tmp_path, "other")
        with dispatch.hold(tmp_path, "r") as second:
            assert not second
        assert dispatch.start(tmp_path, tmp_path, "r", popen=_no_popen) is False
    assert not dispatch.held(tmp_path, "r")


def _no_popen(*a, **kw):
    raise AssertionError("must not start")


def test_start_launches_a_detached_dispatcher_without_the_session_s_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUCE_SESSION_ID", "the-session")
    started = []
    assert dispatch.start(tmp_path, tmp_path, "r", popen=lambda argv, **kw: started.append((argv, kw)))
    argv, kw = started[0]
    assert argv[1:] == ["-m", "cauce", "work", "--repo", str(tmp_path)] and kw["start_new_session"]
    assert "CAUCE_SESSION_ID" not in kw["env"] and str(Path(dispatch.__file__).parents[1]) in kw["env"]["PYTHONPATH"]

    def broken(*a, **kw):
        raise OSError("no fork")

    assert dispatch.start(tmp_path, tmp_path, "s", popen=broken) is False
    assert "CAUCE_SESSION_ID" not in launch.worker_env()


def test_double_plus_starts_the_dispatcher_when_autowork_is_on(store: Store, git_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: calls.append(repo_dir) or True)
    event = {"session_id": "s", "prompt_id": "1", "cwd": str(git_repo), "prompt": "++ write the changelog"}
    assert "`cauce work` runs the queue" in hooks.user_prompt_submit(event, store)["reason"] and not calls
    monkeypatch.setenv("CAUCE_AUTOWORK", "on")
    reason = hooks.user_prompt_submit({**event, "prompt_id": "2"}, store)["reason"]
    assert calls == [git_repo] and "when the tasks ahead of it are done" in reason
    monkeypatch.setenv("CAUCE_PARALLEL", "on")
    assert "beside the running ones" in hooks.user_prompt_submit({**event, "prompt_id": "3"}, store)["reason"]
    monkeypatch.setattr(dispatch, "start", lambda *a: False)
    assert "Starting a worker failed" in hooks.user_prompt_submit({**event, "prompt_id": "4"}, store)["reason"]
    assert all(t["session_id"] == "s" for t in store.list_tasks(status=["queued"]))


def test_a_task_queued_by_hand_starts_the_dispatcher_too(git_repo, monkeypatch, capsys):
    """A session that queued with `cauce queue add` waited for a `cauce work`
    nobody ran: the task sat queued."""
    calls = []
    monkeypatch.setattr(dispatch, "start", lambda repo_dir, root, scope: calls.append(repo_dir) or True)
    monkeypatch.setenv("CAUCE_AUTOWORK", "on")
    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo)]) == 0
    assert calls == [git_repo.resolve()] and "A worker takes it" in capsys.readouterr().out


def test_a_task_process_that_dies_unannounced_is_interrupted(store: Store):
    task = store.enqueue("x", repo="r", cwd="/x")
    store.claim(task["id"])
    handle = object.__new__(flow._Process)
    handle.task_id, handle.store = task["id"], store
    handle.proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    handle.proc.wait()
    assert handle.poll() == "interrupted"
    assert store.get_task(task["id"])["status"] == "interrupted" and store.lanes()[0]["paused"]
    stop = store.last_event(task["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "died" and stop["by"] == "cauce" and "without saying how" in stop["reason"]
    store.update_task(task["id"], status="done")
    assert handle.poll() == "done"
    store.update_task(task["id"], status="running")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    handle.proc = sleeper
    assert handle.poll() is None
    sleeper.kill()
    sleeper.wait()


def test_a_task_process_runs_through_cauce_run_queued(store: Store, git_repo, monkeypatch, capsys):
    task = store.enqueue("write the docs", repo=str(git_repo.resolve()), cwd=str(git_repo),
                         options={"kind": "docs"})
    assert cli.main(["run-queued", str(task["id"])]) == 1  # not claimed by a dispatcher
    assert "not a task the dispatcher claimed" in capsys.readouterr().err
    store.claim(task["id"])
    seen = {}

    def fake_run(text, repo_dir, st, options, **kw):
        seen.update(text=text, kind=options.kind, task_id=kw["task_id"], model=options.use_model_classifier)
        return type("R", (), {"status": "done", "text": lambda self: "task: done"})()

    monkeypatch.setattr(cli.orchestrate, "run", fake_run)
    assert cli.main(["run-queued", str(task["id"]), "--no-model"]) == 0
    assert seen == {"text": "write the docs", "kind": "docs", "task_id": task["id"], "model": False}


def test_the_process_handle_starts_run_queued(store: Store, tmp_path, monkeypatch):
    task = store.enqueue("x", repo="r", cwd=str(tmp_path))
    store.claim(task["id"])
    started = {}

    class Proc:
        def __init__(self, argv, **kw):
            started.update(argv=argv, **kw)

    monkeypatch.setattr(flow.subprocess, "Popen", Proc)
    flow._Process(store.get_task(task["id"]), tmp_path, store, flow.Options(use_model_classifier=False))
    assert started["argv"][1:] == ["-m", "cauce", "run-queued", str(task["id"]), "--no-model"]
    assert started["cwd"] == str(tmp_path)


def test_a_second_dispatcher_for_the_same_repository_steps_aside(git_repo, capsys):
    from cauce.store import home

    key = str(git_repo.resolve())
    with dispatch.hold(home(), key):
        assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert "already runs" in capsys.readouterr().out
    assert cli.main(["work", "--repo", str(git_repo)]) == 0
    assert "nothing runnable" in capsys.readouterr().out


def test_a_run_or_queue_typed_in_a_session_is_that_session_s(git_repo, monkeypatch, capsys):
    monkeypatch.setenv("CAUCE_SESSION_ID", "sess-1")
    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo), "--json"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["then"] == "`cauce work` runs the queue."  # autowork is off in tests
    store = Store.open()
    assert store.get_task(queued["id"])["session_id"] == "sess-1"
    store.close()
    seen = {}
    monkeypatch.setattr(cli.orchestrate, "run", lambda *a, **kw: seen.update(kw) or type(
        "R", (), {"status": "done", "text": lambda self: ""})())
    assert cli.main(["run", "x", "--repo", str(git_repo)]) == 0
    assert seen["session_id"] == "sess-1"


def test_without_an_env_file_nothing_is_exported(tmp_path):
    assert hooks._export({}, "X", "1") is False
    assert hooks._export({"CLAUDE_ENV_FILE": str(tmp_path / "no" / "such")}, "X", "1") is False
