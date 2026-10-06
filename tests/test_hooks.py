from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

from cauce import hooks, stops
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
    assert "stopped for a person" not in context
    blocked = store.create_task("create the routes", status="running", source="cauce", session_id="s", cwd="/x")
    stops.record(store, blocked["id"], stops.Stop("permission", "the worker was refused Bash(node a.js)",
                                                  denied=("Bash(node a.js)",)))
    context = hooks.session_start(event, store, root)["hookSpecificOutput"]["additionalContext"]
    assert f"#{blocked['id']} [blocked] create the routes" in context
    assert "stopped by your permission settings: the worker was refused Bash(node a.js)" in context
    assert f"cauce resume {blocked['id']} --allow 'Bash(node:*)'" in context
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


def test_session_start_freshens_an_adopted_index_in_the_background(store, git_repo, tmp_path):
    from cauce.adapters import ABSENT, PRESENT, Status

    class Fake:
        name = "livespec"

        def __init__(self, state, stale=False):
            self.state, self.stale, self.started = state, stale, []

        def inspect(self, repo_dir):
            return Status(self.name, self.state, stale=self.stale)

        def server(self):
            return {"command": "livespec"}

        def refresh_in_background(self, repo_dir, log):
            self.started.append(log)
            return True

    root = tmp_path / "root"
    event = {"session_id": "s", "cwd": str(git_repo), "source": "startup"}
    for fake, word in ((Fake(ABSENT), "indexing"), (Fake(PRESENT, stale=True), "refreshing")):
        answer = hooks.session_start(event, store, root, {}, adapters=[fake])
        assert word in answer["hookSpecificOutput"]["additionalContext"] and fake.started
    quiet = Fake(PRESENT)
    assert hooks.session_start(event, store, root, {}, adapters=[quiet]) is None and not quiet.started
    outside = Fake(ABSENT)
    hooks.session_start({**event, "cwd": str(tmp_path)}, store, root, {}, adapters=[outside])
    assert not outside.started  # never index a directory that is not a repository


def test_session_start_copies_plugin_options(store, tmp_path):
    from cauce import config

    env = {"CAUCE_HOME": str(tmp_path / "h"), "CLAUDE_PLUGIN_OPTION_LIVESPEC": "false"}
    hooks.session_start({"session_id": "s"}, store, tmp_path / "h", env, adapters=[])
    assert config.load(env)["livespec"] is False


def test_session_start_puts_cauce_on_the_sessions_path(store, tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "bin" / "cauce").write_text("#!/bin/sh\n")
    (plugin / "bin" / "cauce").chmod(0o755)
    env_file = tmp_path / "session.env"
    env = {"CLAUDE_PLUGIN_ROOT": str(plugin), "PATH": "/usr/bin", "CLAUDE_ENV_FILE": str(env_file)}
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", env, adapters=[]) is None
    first, line = env_file.read_text().splitlines(keepends=True)
    assert first == "export CAUCE_SESSION_ID=s\n"  # `cauce run` typed in the session is that session's
    assert line.startswith("export PATH=") and str(plugin / "bin") in line and line.endswith(':"$PATH"\n')
    # Already resolvable: the path is not written twice.
    on_path = {**env, "PATH": str(plugin / "bin")}
    hooks.session_start({"session_id": "s"}, store, tmp_path / "h", on_path, adapters=[])
    assert env_file.read_text().count("export PATH=") == 1
    # No env file: the model is given the full path.
    bare = {"CLAUDE_PLUGIN_ROOT": str(plugin), "PATH": "/usr/bin"}
    context = hooks.session_start({"session_id": "s"}, store, tmp_path / "h", bare, adapters=[])
    assert str(plugin / "bin" / "cauce") in context["hookSpecificOutput"]["additionalContext"]
    unwritable = {**bare, "CLAUDE_ENV_FILE": str(tmp_path / "missing" / "x.env")}
    context = hooks.session_start({"session_id": "s"}, store, tmp_path / "h", unwritable, adapters=[])
    assert "not on PATH" in context["hookSpecificOutput"]["additionalContext"]
    # Not a plugin run, or a plugin without the launcher: nothing to do.
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", {"PATH": "/usr/bin"}, adapters=[]) is None
    hollow = {**bare, "CLAUDE_PLUGIN_ROOT": str(tmp_path)}
    assert hooks.session_start({"session_id": "s"}, store, tmp_path / "h", hollow, adapters=[]) is None


def test_the_launcher_follows_a_symlink_and_names_a_missing_python(tmp_path):
    link = tmp_path / "cauce"
    link.symlink_to(ROOT / "bin" / "cauce")
    env = {**os.environ, "CAUCE_HOME": str(tmp_path / "h")}
    proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout
    proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, check=False,
                          env={**env, "CAUCE_PYTHON": "no-such-python3"})
    assert proc.returncode == 127 and "no-such-python3 not found" in proc.stderr
    old = shutil.which("python3.10")
    if old:
        # A person's choice is kept: an interpreter too old says so in one line,
        # not an import traceback.
        proc = subprocess.run([str(link), "matrix"], capture_output=True, text=True, check=False,
                              env={**env, "CAUCE_PYTHON": old})
        assert proc.returncode == 1 and "needs Python 3.11+" in proc.stderr and "Traceback" not in proc.stderr
        # A bare old python3 hands over to a newer one beside it.
        if any(shutil.which(name) for name in ("python3.11", "python3.12", "python3.13", "python3.14")):
            bare = {k: v for k, v in env.items() if k != "CAUCE_PYTHON"}
            proc = subprocess.run([old, "-m", "cauce", "matrix"], capture_output=True, text=True, check=False,
                                  env={**bare, "PYTHONPATH": str(ROOT / "src")})
            assert proc.returncode == 0 and "debug-unclear" in proc.stdout


def test_double_plus_queues_without_a_turn(store: Store, git_repo):
    answer = prompt(store, "++ write the release notes", cwd=str(git_repo))
    assert answer["decision"] == "block" and "queued #" in answer["reason"]
    task = store.list_tasks()[0]
    assert task["status"] == "queued" and task["body"] == "write the release notes"
    assert task["repo"] == str(git_repo.resolve())
    assert prompt(store, "++", prompt_id="p2")["reason"].startswith('cauce: "++ <task>"')


def test_a_turn_with_a_subagent_still_running_is_not_done(store: Store):
    prompt(store, "investigate the leak", prompt_id="p1")
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                        "tool_input": {"description": "look at the pool", "prompt": "check the pool",
                                       "run_in_background": True}}, store)
    hooks.post_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "tu1",
                         "tool_input": {"run_in_background": True}, "tool_response": "launched"}, store)
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "waiting on the agent"}, store)
    parent = next(t for t in store.list_tasks() if t["source"] == "hook")
    child = next(t for t in store.list_tasks() if t["source"] == "delegation")
    assert store.messages(child["id"])[0]["role"] == "orchestrator"  # the main session wrote it
    assert parent["status"] == "running" and child["status"] == "running" and child["parent_id"] == parent["id"]
    note = ("<task-notification><tool-use-id>tu1</tool-use-id><status>completed</status>"
            "<result>found it</result></task-notification>")
    prompt(store, note, prompt_id="p2")
    assert store.get_task(child["id"])["result"] == "found it"
    assert store.get_task(parent["id"])["status"] == "done"
    assert len([t for t in store.list_tasks() if t["source"] == "hook"]) == 1  # a notification is no task


def test_a_foreground_subagent_finishes_with_its_tool_call(store: Store):
    prompt(store, "review it", prompt_id="p1")
    hooks.pre_tool_use({"session_id": "s", "tool_name": "Task", "tool_use_id": "tu2",
                        "tool_input": {"prompt": "review"}}, store)
    hooks.post_tool_use({"session_id": "s", "tool_name": "Task", "tool_use_id": "tu2",
                         "tool_input": {}, "tool_response": {"content": "lgtm"}}, store)
    child = next(t for t in store.list_tasks() if t["source"] == "delegation")
    assert child["status"] == "done" and "lgtm" in child["result"]
    assert hooks.pre_tool_use({"session_id": "s", "tool_name": "Bash"}, store) is None
    assert hooks.post_tool_use({"session_id": "s", "tool_name": "Agent", "tool_use_id": "nope"}, store) is None
    prompt(store, "<task-notification><tool-use-id>missing</tool-use-id></task-notification>", prompt_id="p9")


def test_stop_counts_what_the_turn_cost(store: Store, tmp_path):
    from .test_usage import line

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(line("m1", out=42))
    prompt(store, "do a thing", prompt_id="p1")
    hooks.stop({"session_id": "s", "prompt_id": "p1", "last_assistant_message": "ok",
                "transcript_path": str(transcript)}, store)
    assert store.usage_by_model(days=100000)[0]["output_tokens"] == 42


def test_work_a_session_sent_is_reported_to_it_once_when_it_ends(store: Store, git_repo):
    """A queued task finished while the main session waited on nothing: nothing
    told it to tell the person, continue or relaunch."""
    from cauce import stops

    done = store.create_task("write the changelog", status="running", source="cauce", session_id="s",
                             cwd=str(git_repo), final_cell="sonnet/low")
    store.add_event(done["id"], "finished", status="done", branch="cauce/task-1", final_cell="sonnet/low",
                    changed=["CHANGELOG.md"])
    store.update_task(done["id"], status="done")
    big = store.create_task("reduce the app to four modules", status="running", source="cauce", session_id="s",
                            cwd=str(git_repo))
    stops.record(store, big["id"], stops.Stop("turns", "the task does not fit even the raised turn budget"))
    store.add_event(big["id"], "finished", status="replan", changed=[f"src/f{i}.ts" for i in range(7)],
                    stop=stops.Stop("turns", "the task does not fit even the raised turn budget").data())
    other = store.create_task("not this session's", status="failed", source="cauce", session_id="t")
    store.add_event(other["id"], "finished", status="failed")
    store.create_task("still going", status="running", source="cauce", session_id="s")

    out = hooks.stop({"session_id": "s"}, store)
    assert out["decision"] == "block"
    notice = out["reason"]
    assert "#1 [done] write the changelog" in notice and "git diff HEAD...cauce/task-1" in notice
    assert f"#{big['id']} [replan]" in notice and "stopped by cauce's rules" in notice
    assert "src/f0.ts, src/f1.ts, src/f2.ts, src/f3.ts, src/f4.ts and 2 more" in notice
    assert f"cauce resume {big['id']} --max-turns 120" in notice
    assert "not this session's" not in notice and "still going" not in notice
    assert hooks.stop({"session_id": "s"}, store) is None  # once
    # a later ending reaches the next prompt instead
    store.add_event(big["id"], "finished", status="replan",
                    stop=stops.Stop("turns", "again").data())
    context = prompt(store, "what is next for the app", session="s")["hookSpecificOutput"]["additionalContext"]
    assert f"#{big['id']} [replan]" in context and "#1 [done]" not in context
    assert prompt(store, "and after that", prompt_id="p2", session="s") is None


def test_an_old_ending_is_not_brought_back(store: Store):
    t = store.create_task("long ago", status="done", source="cauce", session_id="s")
    store._conn.execute("INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, 'finished', '{}')",
                        (t["id"], "2020-01-01T00:00:00+00:00"))
    assert store.unreported("s") == []
