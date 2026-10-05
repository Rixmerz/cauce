from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from cauce import hooks
from cauce.store import Store

ROOT = Path(__file__).resolve().parents[1]


def prompt(store, text, prompt_id="p1", session="s", cwd=None):
    return hooks.user_prompt_submit({"session_id": session, "prompt": text, "prompt_id": prompt_id, "cwd": cwd},
                                    store)


def test_a_prompt_is_a_task_and_a_follow_up_joins_it(store: Store, git_repo):
    prompt(store, "fix the login redirect", cwd=str(git_repo))
    prompt(store, "and keep the query string", prompt_id="p1")
    tasks = store.list_tasks()
    assert len(tasks) == 1 and tasks[0]["repo"] == str(git_repo.resolve())
    assert [m["text"] for m in store.messages(tasks[0]["id"])] == ["fix the login redirect",
                                                                    "and keep the query string"]


def test_stop_writes_the_result_and_interrupts_abandoned_turns(store: Store):
    prompt(store, "first thing", prompt_id="p1")
    prompt(store, "second thing", prompt_id="p2")
    hooks.stop({"session_id": "s", "prompt_id": "p2", "last_assistant_message": "done"}, store)
    first, second = sorted(store.list_tasks(), key=lambda t: t["id"])
    assert second["status"] == "done" and second["result"] == "done"
    assert first["status"] == "interrupted"
    assert store.messages(second["id"])[-1] == {**store.messages(second["id"])[-1], "role": "assistant"}


def test_a_bare_continue_folds_into_the_interrupted_turn(store: Store):
    prompt(store, "migrate the users table", prompt_id="p1")
    store.interrupt_running("s")
    prompt(store, "Sigue", prompt_id="p2")
    tasks = store.list_tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "running" and tasks[0]["prompt_id"] == "p2"


def test_harness_turns_and_empty_prompts_are_not_tasks(store: Store):
    prompt(store, "<task-notification>done</task-notification>")
    prompt(store, "   ")
    assert hooks.user_prompt_submit({"prompt": "no session"}, store) is None
    assert store.list_tasks() == []


def test_known_dead_ends_reach_the_model_before_it_starts(store: Store):
    p = store.open_problem("websocket reconnect loop", repo="github.com/x/y")
    store.add_fix(p, "raise the backoff to 30s", "failed", repo="github.com/x/y", why="loop is client side")
    answer = prompt(store, "the websocket reconnect loop is back in the dashboard")
    context = answer["hookSpecificOutput"]["additionalContext"]
    assert answer["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "raise the backoff to 30s" in context and "github.com/x/y" in context
    assert prompt(store, "rename the button", prompt_id="p9") is None
    assert prompt(store, "ok", prompt_id="p10") is None


def test_stop_failure_and_missing_sessions(store: Store):
    prompt(store, "deploy staging")
    hooks.stop_failure({"session_id": "s", "error": "rate limited"}, store)
    assert store.list_tasks()[0]["status"] == "failed"
    assert hooks.stop({}, store) is None and hooks.stop_failure({}, store) is None
    assert hooks.stop({"session_id": "s"}, store) is None


def test_session_start_lists_unfinished_work_dead_ends_and_swallowed_errors(store: Store, git_repo, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    prompt(store, "half done thing", cwd=str(git_repo))
    p = store.open_problem("flaky import", repo=str(git_repo.resolve()))
    store.add_fix(p, "pin the version", "failed", repo=str(git_repo.resolve()))
    (root / hooks.ERROR_LOG).write_text("--- a Stop\nboom\n\n--- b Stop\nboom\n")
    event = {"session_id": "s", "cwd": str(git_repo), "source": "resume"}
    context = hooks.session_start(event, store, root)["hookSpecificOutput"]["additionalContext"]
    assert "2 hook error(s)" in context and "half done thing" in context and "pin the version" in context
    again = hooks.session_start({**event, "source": "startup"}, store, root)["hookSpecificOutput"]
    assert "hook error" not in again["additionalContext"]
    assert hooks.session_start({}, store, root) is None
    assert hooks.session_start({"session_id": "z", "cwd": str(tmp_path / "nowhere")}, store, root) is None


def test_main_answers_on_stdout_and_never_raises(tmp_path):
    env = {"CAUCE_HOME": str(tmp_path / "h")}
    out = io.StringIO()
    event = {"session_id": "s", "prompt": "write the docs", "prompt_id": "1"}
    assert hooks.main("UserPromptSubmit", io.StringIO(json.dumps(event)), out, env) == 0
    assert out.getvalue() == ""
    assert hooks.main("Unknown", io.StringIO("{}"), out, env) == 0
    assert hooks.main("Stop", io.StringIO("[]"), out, env) == 0
    assert hooks.main("Stop", io.StringIO("not json"), out, env) == 0
    assert "JSONDecodeError" in (tmp_path / "h" / hooks.ERROR_LOG).read_text()
    assert hooks.main("Stop", io.StringIO("not json"), out, {**env, "CAUCE_HOOKS_OFF": "1"}) == 0
    assert (tmp_path / "h" / hooks.ERROR_LOG).read_text().count("--- ") == 1


def test_the_launcher_runs_a_hook_as_its_own_process(tmp_path):
    """The contract is 'never take the session down', so it is tested the way
    Claude Code runs it: a separate interpreter, through bin/cauce."""
    env = {**os.environ, "CAUCE_HOME": str(tmp_path / "h")}
    proc = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "SessionStart"], input="garbage",
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout == ""
    event = json.dumps({"session_id": "s", "prompt": "add a cache layer to the api", "prompt_id": "1"})
    proc = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "UserPromptSubmit"], input=event,
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0
    store = Store(tmp_path / "h" / "cauce.db")
    assert store.list_tasks()[0]["title"] == "add a cache layer to the api"
    store.close()
