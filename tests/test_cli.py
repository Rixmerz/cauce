from __future__ import annotations

import errno
import json

import pytest

from cauce import cli, orchestrate, stops
from cauce.launch import WorkerResult
from cauce.store import Store


def test_matrix_and_classify(capsys):
    assert cli.main(["matrix"]) == 0
    assert "debug-unclear    opus/high → opus/xhigh → opus/max" in capsys.readouterr().out
    assert cli.main(["classify", "haz commit y push", "--no-model"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "docs"


def test_route_is_a_dry_run(capsys, git_repo):
    assert cli.main(["route", "where is the router defined?", "--repo", str(git_repo), "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "plan: dry run" in out and "kind explore" in out and "start haiku" in out


def test_run_reports_and_fails_on_a_non_pass(capsys, git_repo, monkeypatch):
    def fake_run(text, repo_dir, store, options, **kw):
        kw["launcher"] = lambda spec: WorkerResult(False, None, "nope")
        kw["registry"] = {}
        return real(text, repo_dir, store, options, **kw)

    real = orchestrate.run
    monkeypatch.setattr(orchestrate, "run", fake_run)
    code = cli.main(["run", "fix it", "--repo", str(git_repo), "--no-model", "--kind", "docs",
                     "--max-attempts", "1", "--start", "haiku"])
    out, err = capsys.readouterr()
    assert code == 1 and "task #1: failed" in out
    # progress goes to stderr as it happens, so stdout stays the report alone
    assert err.splitlines()[:2] == ["cauce: task #1 started: docs, at haiku/medium",
                                    "cauce: #1 attempt 1 at haiku/medium"]
    assert "cauce: task #1 started" not in out


def test_tasks_show_and_memory(capsys, git_repo):
    store = Store.open()
    t = store.create_task("a task", status="done", source="cauce", repo="github.com/o/r", kind="docs")
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=0, failure="code_bug", summary="missed")
    store.set_move(t["id"], 1, "more_effort", "shallow")
    store.close()
    assert cli.main(["tasks", "--all"]) == 0
    assert "a task" in capsys.readouterr().out
    assert cli.main(["show", str(t["id"])]) == 0
    out = capsys.readouterr().out
    assert "attempt 1 haiku" in out and "more_effort" in out and "missed" in out
    assert cli.main(["show", "999"]) == 1
    store = Store.open()
    blocked = store.create_task("routes", status="running", source="cauce", repo="github.com/o/r", cwd=str(git_repo))
    store.add_attempt(blocked["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission")
    store.add_event(blocked["id"], "attempt_finished", seq=1, denied=["Bash(node a.js)"])
    stops.record(store, blocked["id"], stops.Stop("permission", "the worker was refused Bash(node a.js)",
                                                  denied=("Bash(node a.js)",)))
    store.close()
    assert cli.main(["show", str(blocked["id"])]) == 0
    out = capsys.readouterr().out
    assert "refused: Bash(node a.js)" in out
    assert "stopped by your permission settings: the worker was refused" in out
    assert f"cauce resume {blocked['id']} --allow 'Bash(node:*)'" in out
    store = Store.open()
    store.set_move(blocked["id"], 1, "more_effort", "code_bug: same model, more thorough")
    store.add_event(blocked["id"], "moved", seq=1, from_cell="sonnet/medium", to_cell="sonnet/high", axis="effort",
                    because=["attempt 1 ended code_bug", "next: sonnet/high"], skipped=["sonnet/xhigh: why"])
    store.close()
    assert cli.main(["show", str(blocked["id"])]) == 0
    out = capsys.readouterr().out
    assert "effort: sonnet/medium → sonnet/high" in out and "· next: sonnet/high" in out
    assert "· skipped sonnet/xhigh: why" in out

    assert cli.main(["memory", "record", "--problem", "slow startup", "--fix", "lazy import pandas",
                     "--outcome", "failed", "--why", "still slow", "--repo", str(git_repo)]) == 0
    assert cli.main(["memory", "search", "slow startup", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "lazy import pandas" in out and "still slow" in out
    assert cli.main(["memory", "search", "nothing like it"]) == 0
    assert "nothing recorded" in capsys.readouterr().out


def test_config_model_shows_pins_and_what_served(capsys):
    store = Store.open()
    store.add_usage(message_id="m1", session_id="s", model="claude-sonnet-5-5", output_tokens=1, ts="2099-01-01")
    t = store.create_task("x", status="done", source="cauce")
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1, served_model="claude-sonnet-5")
    store.close()
    assert cli.main(["config", "model"]) == 0
    out = capsys.readouterr().out
    assert "sonnet  follows Claude Code; workers last ran claude-sonnet-5; your sessions use claude-sonnet-5-5" in out
    assert "opus    follows Claude Code" in out
    assert cli.main(["config", "model", "sonnet", "claude-sonnet-5-5"]) == 0
    assert capsys.readouterr().out.startswith("sonnet  pinned to claude-sonnet-5-5; workers last ran")
    assert cli.main(["config", "model", "sonnet", "default"]) == 0
    assert "follows Claude Code" in capsys.readouterr().out
    assert cli.main(["config", "model", "gpt"]) == 1
    assert cli.main(["config", "model", "opus", "two words"]) == 1
    assert cli.main(["show", str(t["id"])]) == 0
    assert "attempt 1 sonnet/medium [claude-sonnet-5]" in capsys.readouterr().out


def test_init_refuses_the_home_directory(capsys, tmp_path, monkeypatch):
    from cauce import project

    monkeypatch.setattr(project.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["init", str(tmp_path)]) == 1
    assert "is not a project" in capsys.readouterr().err and not (tmp_path / ".cauce").exists()


def test_allow_keeps_rules_for_the_repository_and_config_mode_sets_modes(capsys, git_repo):
    assert cli.main(["allow", "--repo", str(git_repo)]) == 0
    assert "nothing beyond their mode" in capsys.readouterr().out
    assert cli.main(["allow", "Bash(make:*)", "--preset", "node", "--repo", str(git_repo)]) == 0
    assert "Bash(npm:*), Bash(npx:*), Bash(node:*), Bash(make:*)" in capsys.readouterr().out
    assert cli.main(["allow", "--rm", "Bash(make:*)", "--repo", str(git_repo)]) == 0
    assert "Bash(make:*)" not in capsys.readouterr().out
    assert cli.main(["allow", "--preset", "nope", "--repo", str(git_repo)]) == 1
    plain = git_repo.parent / "plain"
    plain.mkdir()
    assert cli.main(["allow", "--repo", str(plain)]) in (0, 1)
    capsys.readouterr()

    assert cli.main(["config", "mode"]) == 0
    out = capsys.readouterr().out
    assert "haiku   bypassPermissions" in out and "sonnet  auto" in out
    assert cli.main(["config", "mode", "haiku", "acceptEdits"]) == 0
    assert capsys.readouterr().out.strip() == "haiku   acceptEdits"
    assert cli.main(["config", "mode", "default", "dontAsk"]) == 0
    assert "opus    dontAsk" in capsys.readouterr().out
    assert cli.main(["config", "mode", "haiku", "reset"]) == 0
    assert "bypassPermissions" in capsys.readouterr().out
    assert cli.main(["config", "mode", "gpt"]) == 1
    assert cli.main(["config", "mode", "opus", "plan"]) == 1


def test_capabilities(capsys, _isolated_home):
    assert cli.main(["capabilities"]) == 0
    assert "no capabilities registered" in capsys.readouterr().out
    assert cli.main(["capabilities", "--example"]) == 0
    example = capsys.readouterr().out
    _isolated_home.mkdir(parents=True, exist_ok=True)
    (_isolated_home / "capabilities.json").write_text(example)
    assert cli.main(["capabilities"]) == 0
    out = capsys.readouterr().out
    assert "livespec" in out and "after a failed implement" in out


def test_config_and_neighbours(capsys, git_repo):
    assert cli.main(["config"]) == 0
    assert "livespec = true" in capsys.readouterr().out
    assert cli.main(["config", "livespec", "off"]) == 0
    assert cli.main(["config", "livespec"]) == 0
    assert capsys.readouterr().out.strip().endswith("false")
    assert cli.main(["config", "nope"]) == 1
    assert cli.main(["config", "livespec", "maybe"]) == 1
    assert cli.main(["neighbours", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "livespec setting: off" in out and "livespec: absent" in out


def test_cancel_signals_the_run_and_events_print(capsys, monkeypatch):
    import signal

    store = Store.open()
    running = store.create_task("long job", status="running", source="cauce", pid=4242)
    queued = store.create_task("later", status="queued", source="cauce")
    done = store.create_task("old", status="done", source="cauce")
    store.add_event(running["id"], "planned", kind="implement")
    store.close()
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert cli.main(["cancel", str(running["id"])]) == 0
    assert killed == [(4242, signal.SIGTERM)]
    assert cli.main(["cancel", str(queued["id"])]) == 0
    assert cli.main(["cancel", str(done["id"])]) == 0
    assert "nothing to cancel" in capsys.readouterr().out
    assert cli.main(["cancel", "999"]) == 1
    store = Store.open()
    assert store.get_task(queued["id"])["status"] == "cancelled"
    assert "with `cauce cancel`" in store.last_event(queued["id"], "finished")["data"]["stop"]["reason"]
    assert store.last_event(running["id"], "cancel_requested")["data"] == {"via": "cli"}
    store.close()

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(cli.os, "kill", gone)
    store = Store.open()
    again = store.create_task("x", status="running", source="cauce", pid=1)
    store.close()
    assert cli.main(["cancel", str(again["id"])]) == 0

    assert cli.main(["events"]) == 0
    assert '"kind": "implement"' in capsys.readouterr().out
    assert cli.main(["events", "--json", "--task", str(running["id"])]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["kind"] == "planned"
    assert cli.main(["events", "--json", "--kind", "cancel_requested"]) == 0
    assert {json.loads(x)["kind"] for x in capsys.readouterr().out.splitlines()} == {"cancel_requested"}
    assert cli.main(["events", "--new"]) == 0
    assert capsys.readouterr().out == ""


def test_sigterm_becomes_a_cancel():
    import signal

    import pytest

    previous = cli._cancel_on_sigterm()
    try:
        with pytest.raises(orchestrate.Cancelled):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_queue_lanes_and_work(capsys, git_repo, monkeypatch):
    assert cli.main(["queue", "add", "write docs", "--repo", str(git_repo), "--verify", "true",
                     "--kind", "docs"]) == 0
    assert "queued #1" in capsys.readouterr().out
    assert cli.main(["queue", "list", "--repo", str(git_repo)]) == 0
    assert "write docs" in capsys.readouterr().out
    assert cli.main(["queue"]) == 0
    assert cli.main(["queue", "add", "second", "--repo", str(git_repo)]) == 0
    assert cli.main(["queue", "rm", "2"]) == 0
    assert cli.main(["queue", "rm", "2"]) == 1
    store = Store.open()
    assert store.last_event(2, "finished")["data"]["stop"]["by"] == "you"
    store.close()

    seen = {}

    def fake_work(store, **kw):
        seen.update(kw)
        from cauce.flow import WorkReport
        return WorkReport([], "nothing runnable is queued")

    monkeypatch.setattr(cli.flow, "work", fake_work)
    assert cli.main(["work", "--repo", str(git_repo), "--max", "3"]) == 0
    assert seen["max_tasks"] == 3 and seen["repo"] == str(git_repo.resolve())
    assert cli.main(["work", "--all"]) == 0 and seen["repo"] is None

    store = Store.open()
    store.pause_lane(str(git_repo.resolve()), "task #1 ended failed")
    store.close()
    capsys.readouterr()
    assert cli.main(["lanes"]) == 0
    assert "paused: task #1 ended failed" in capsys.readouterr().out
    assert cli.main(["lanes", "--unpause", str(git_repo)]) == 0
    assert cli.main(["lanes", "--unpause", "github.com/o/r"]) == 0
    assert cli.main(["lanes"]) == 0
    assert "open" in capsys.readouterr().out


def test_spend_and_invalidate(capsys):
    store = Store.open()
    t = store.create_task("x", status="done", source="cauce", repo="r", kind="docs")
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=1, cost_usd=0.1)
    p = store.open_problem("p", repo="r")
    fix = store.add_fix(p, "f", "worked", repo="r")
    store.close()
    assert cli.main(["spend", "--days", "3"]) == 0
    assert "haiku" in capsys.readouterr().out
    assert cli.main(["spend", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["workers"][0]["cell"] == "haiku"
    assert cli.main(["memory", "invalidate", str(fix), "--why", "came back"]) == 0
    assert "recurring" in capsys.readouterr().out
    assert cli.main(["memory", "invalidate", "999", "--why", "x"]) == 1
    assert cli.main(["memory", "record", "--problem", "q", "--fix", "g", "--outcome", "worked",
                     "--commit", "abc"]) == 0


def test_habits_commands(capsys, tmp_path, monkeypatch):
    store = Store.open()
    for s in range(3):
        for sig in ("edit:.py", "bash:ruff-format"):
            store.add_tool_event(session_id=f"s{s}", tool="x", sig=sig, arg_hash="h", ok=1)
    store.close()
    assert cli.main(["habits"]) == 0
    line = capsys.readouterr().out.strip()
    cid = line.split()[0]
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(repo_dir)]) == 0
    assert "installed habit #1" in capsys.readouterr().out
    assert cli.main(["habits", "install", "nope", "--command", "true", "--repo", str(repo_dir)]) == 1
    assert cli.main(["habits", "status"]) == 0
    assert "edit:.py → bash:ruff-format" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"tool_input": {"file_path": "a.py"}})))
    assert cli.main(["habit-run", "1"]) == 0
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("garbage"))
    assert cli.main(["habit-run", "1"]) == 0
    assert cli.main(["habits", "uninstall", "1"]) == 0
    assert cli.main(["habits", "uninstall", "1"]) == 0  # already removed from settings: still fine
    assert cli.main(["habits", "uninstall", "99"]) == 1
    (repo_dir / ".claude" / "settings.local.json").write_text("{broken")
    assert cli.main(["habits", "install", cid, "--command", "true", "--repo", str(repo_dir)]) == 1
    store = Store.open()
    store._conn.execute("DELETE FROM tool_events")
    store.close()
    assert cli.main(["habits", "list", "--days", "1"]) == 0


def test_board_counts_for_a_status_line(capsys):
    store = Store.open()
    store.create_task("x", status="failed", source="cauce")
    store.enqueue("y", repo="r", cwd="/x")
    store.close()
    assert cli.main(["board", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"needs_you": 1, "running": 0, "workers": 0, "queued": 1, "done": 0,
                                                   "answering": 0}
    assert cli.main(["board"]) == 0
    assert capsys.readouterr().out.strip() == "cauce ⚠1 ▶0 ⏸1"


def test_the_json_surface_a_page_reads(capsys, git_repo):
    """What another program reads: the board scoped to some repositories, a task, a queued task."""
    from cauce import repo

    assert cli.main(["queue", "add", "write the docs", "--repo", str(git_repo), "--json"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["status"] == "queued" and queued["cwd"] == str(git_repo.resolve())
    store = Store.open()
    store.create_task("elsewhere", status="failed", source="cauce", repo="github.com/o/other")
    store.close()

    assert cli.main(["board", "--full", "--repo", str(git_repo)]) == 0
    board = json.loads(capsys.readouterr().out)
    assert board["repos"] == {str(git_repo): repo.key(git_repo.resolve())}
    assert board["counts"] == {"needs_you": 0, "running": 0, "workers": 0, "queued": 1, "done": 0, "answering": 0}
    assert board["queued"][0]["tasks"][0]["id"] == queued["id"]
    assert cli.main(["board", "--full"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["needs_you"] == 1

    assert cli.main(["show", str(queued["id"]), "--json"]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["task"]["title"] == "write the docs" and detail["attempts"] == []
    assert cli.main(["show", "999", "--json"]) == 1


def test_projects_sessions_and_memory_as_json(capsys, git_repo):
    """Where cauce has worked, the sessions it saw there with how to resume them, and the problems
    and fixes it remembers, everything or one repository's."""
    from cauce import repo

    key = repo.key(git_repo.resolve())
    store = Store.open()
    store.touch_session("s-1", str(git_repo), key)
    store.create_task("fix the cart total", status="done", source="hook", session_id="s-1", repo=key,
                      cwd=str(git_repo))
    store.create_task("queued one", status="queued", source="queue", repo=key, cwd=str(git_repo))
    store.touch_session("s-2", "/elsewhere", "github.com/o/other")
    p = store.open_problem("cart total off by one", repo=key)
    store.add_fix(p, "round before summing", "failed", repo=key, why="still off")
    store.add_fix(p, "sum in cents", "worked", repo=key)
    q = store.open_problem("flaky login test", repo="github.com/o/other")
    store.add_fix(q, "retry", "failed", repo="github.com/o/other")
    store.close()

    # Only an enrolled project (a .cauce/ folder) is listed; --all shows the rest.
    assert cli.main(["projects", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["init", str(git_repo)]) == 0 and "enrolled" in capsys.readouterr().out
    assert (git_repo / ".cauce" / ".gitignore").read_text().endswith("*\n")
    assert cli.main(["init", str(git_repo / "nope")]) == 1
    assert cli.main(["projects", "--json"]) == 0
    projects = {x["repo"]: x for x in json.loads(capsys.readouterr().out)}
    assert list(projects) == [key] and projects[key]["enrolled"]
    assert projects[key]["dir"] == str(git_repo.resolve()) and projects[key]["exists"]
    assert projects[key]["sessions"] == 1 and projects[key]["tasks"] == {"queued": 1}
    assert cli.main(["projects", "--json", "--all"]) == 0
    projects = {x["repo"]: x for x in json.loads(capsys.readouterr().out)}
    assert projects["github.com/o/other"]["exists"] is False and not projects["github.com/o/other"]["enrolled"]
    assert cli.main(["projects", "--all"]) == 0
    out = capsys.readouterr().out
    assert "(gone)" in out and "not enrolled" in out

    assert cli.main(["sessions", "--json", "--repo", str(git_repo)]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert [x["id"] for x in sessions] == ["s-1"]
    assert sessions[0]["prompts"] == 1 and sessions[0]["last_prompt"] == "fix the cart total"
    assert sessions[0]["resume"].endswith("&& claude --resume s-1")
    out = (cli.main(["sessions"]), capsys.readouterr().out)[1]
    assert "claude --resume s-1" in out and "s-2" not in out  # /elsewhere is not enrolled

    assert cli.main(["memory", "list", "--json"]) == 0
    assert {x["title"] for x in json.loads(capsys.readouterr().out)} == {"cart total off by one", "flaky login test"}
    assert cli.main(["memory", "list", "--json", "--repo", str(git_repo)]) == 0
    mine = json.loads(capsys.readouterr().out)
    assert [x["state"] for x in mine] == ["solved"] and [f["outcome"] for f in mine[0]["fixes"]] == ["failed", "worked"]
    assert cli.main(["memory", "list", "--json", "--query", "login"]) == 0
    assert [x["title"] for x in json.loads(capsys.readouterr().out)] == ["flaky login test"]
    assert cli.main(["memory", "list", "--json", "--query", "login", "--repo", str(git_repo)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["memory", "list"]) == 0 and "sum in cents" in capsys.readouterr().out


def test_ui_command_serves_until_interrupted(monkeypatch, capsys):
    from cauce.ui import server

    class Fake:
        server_address = ("127.0.0.1", 4321)

        def __init__(self):
            import threading
            self.stop_event = threading.Event()

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(server, "serve", lambda port: Fake())
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert cli.main(["ui", "--open"]) == 0
    assert "http://127.0.0.1:4321/" in capsys.readouterr().out and opened


def test_resume_continues_a_stopped_task_with_what_it_was_refused_granted(capsys, git_repo, monkeypatch):
    calls = []

    def fake_run(text, repo_dir, store, options, **kw):
        calls.append((text, repo_dir, options, kw))
        return orchestrate.Report(kw.get("task_id"), "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate.Report, "text", lambda self: f"task #{self.task_id}: {self.status}")
    store = Store.open()
    t = store.create_task("create the routes", status="blocked", source="cauce", repo="r", cwd=str(git_repo),
                          kind="implement", dispatched=1, cancel_requested=1,
                          options=json.dumps({"verify": "npm test", "allow_tools": ["Bash(npm run build)"]}))
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="permission", summary="refused")
    store.add_event(t["id"], "attempt_finished", seq=1, denied=["Bash(node app.js)"])
    approval = store.create_task("plan it", status="needs_approval", source="cauce", repo="r", cwd=str(git_repo),
                                 kind="plan")
    store.add_event(approval["id"], "moved", move="needs_approval", next_cell="fable/high")
    hook = store.create_task("a prompt", status="interrupted", source="hook", repo="r", cwd=str(git_repo))
    done = store.create_task("finished", status="done", source="cauce", repo="r", cwd=str(git_repo))
    odd = store.create_task("odd", status="failed", source="cauce", repo="r", cwd=str(git_repo), kind="implement")
    store.add_attempt(odd["id"], cell="sonnet/high", max_turns=30, passed=0, failure="no-such-failure")
    store.close()
    monkeypatch.setattr(orchestrate, "run", fake_run)

    assert cli.main(["resume", str(t["id"]), "--allow", "Bash(node:*)", "--no-model"]) == 0
    text, repo_dir, options, kw = calls[-1]
    assert text == "create the routes" and repo_dir == git_repo
    assert options.allow_tools == ("Bash(npm run build)", "Bash(node:*)") and options.verify == "npm test"
    assert options.kind == "implement" and options.start.label == "sonnet/medium"
    assert kw["task_id"] == t["id"] and kw["history"][0].failure.value == "permission"
    assert kw["history"][0].denied == ("Bash(node app.js)",)  # the resumed brief still says what was refused
    assert options.isolate is True
    store = Store.open()
    row = store.get_task(t["id"])
    assert row["dispatched"] == 0 and row["cancel_requested"] == 0
    assert json.loads(row["options"])["allow_tools"] == ["Bash(npm run build)", "Bash(node:*)"]
    store.close()

    assert cli.main(["resume", str(approval["id"])]) == 1
    assert "--allow-approval" in capsys.readouterr().err
    assert cli.main(["resume", str(approval["id"]), "--allow-approval"]) == 0
    assert calls[-1][2].start.label == "fable/high" and calls[-1][2].allow_approval
    assert cli.main(["resume", str(odd["id"]), "--verify", "true", "--budget", "2", "--no-isolate"]) == 0
    assert calls[-1][2].isolate is False
    assert calls[-1][3]["history"][0].failure is None and calls[-1][2].budget_usd == 2
    from cauce import stops

    store = Store.open()
    big = store.create_task("reduce it", status="running", source="cauce", repo="r", cwd=str(git_repo), kind="refactor")
    stops.record(store, big["id"], stops.Stop("turns", "outgrew"))
    wrong = store.create_task("impossible", status="running", source="cauce", repo="r", cwd=str(git_repo))
    stops.record(store, wrong["id"], stops.Stop("spec", "no such API"))
    store.close()
    assert cli.main(["resume", str(big["id"])]) == 0
    assert calls[-1][2].max_turns == 120
    assert cli.main(["resume", str(big["id"]), "--max-turns", "200"]) == 0 and calls[-1][2].max_turns == 200
    assert cli.main(["resume", str(wrong["id"])]) == 1
    assert "rewrite it as a new task" in capsys.readouterr().err
    for refused in (hook["id"], done["id"], 999):
        assert cli.main(["resume", str(refused)]) == 1
    assert "only a task that stopped" in capsys.readouterr().err


def test_queue_add_keeps_the_rules_a_person_granted(capsys, git_repo):
    assert cli.main(["queue", "add", "build it", "--repo", str(git_repo), "--allow", "Bash(npm run build)",
                     "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    task = store.get_task(task_id)
    store.close()
    from cauce import flow
    assert flow.queued_options(task, orchestrate.Options()).allow_tools == ("Bash(npm run build)",)
    store = Store.open()
    assert store.messages(task_id)[0]["role"] == "person"
    store.close()


def test_queue_add_from_a_session_is_the_orchestrator_s(capsys, git_repo, monkeypatch):
    monkeypatch.setenv("CAUCE_SESSION_ID", "s1")
    assert cli.main(["queue", "add", "build it", "--repo", str(git_repo), "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    assert store.messages(task_id)[0]["role"] == "orchestrator"
    store.close()


@pytest.mark.parametrize(("found", "code", "says"), [
    ({"cauce": True, "version": None}, 1, "from before 0.4.1"),
    ({"cauce": True, "version": "0.1.1"}, 1, "0.1.1, started before"),
    ({"cauce": False, "version": None}, 1, "another program"),
    (None, 1, "another program"),
])
def test_ui_on_a_taken_port_says_who_holds_it(monkeypatch, capsys, found, code, says):
    from cauce.ui import server

    def taken(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server, "serve", taken)
    monkeypatch.setattr(server, "occupant", lambda port: found)
    assert cli.main(["ui", "--port", "8791"]) == code
    assert says in capsys.readouterr().err


def test_ui_on_a_port_this_cauce_already_serves_opens_it(monkeypatch, capsys):
    from cauce import __version__
    from cauce.ui import server

    def taken(port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server, "serve", taken)
    monkeypatch.setattr(server, "occupant", lambda port: {"cauce": True, "version": __version__})
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert cli.main(["ui", "--port", "8791", "--open"]) == 0
    assert "already at http://127.0.0.1:8791/" in capsys.readouterr().out and opened

    def broken(port):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(server, "serve", broken)
    with pytest.raises(OSError):
        cli.main(["ui", "--port", "80"])


def test_a_report_the_caller_read_is_not_reported_again(capsys, git_repo, monkeypatch):
    store = Store.open()
    t = store.create_task("x", status="running", source="cauce", session_id="s", cwd=str(git_repo))
    store.close()

    def fake_run(text, repo_dir, store, options, **kw):
        store.add_event(t["id"], "finished", status="done")
        store.update_task(t["id"], status="done")
        return orchestrate.Report(t["id"], "done", None)  # type: ignore[arg-type]

    monkeypatch.setattr(orchestrate, "run", fake_run)
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "task: done")
    monkeypatch.setenv("CAUCE_SESSION_ID", "s")
    assert cli.main(["run", "x", "--repo", str(git_repo), "--no-model"]) == 0
    store = Store.open()
    assert store.unreported("s") == []
    store.close()


def test_an_unattended_resume_never_grants_what_only_a_person_can(capsys, git_repo, monkeypatch):
    """A model may continue work; it may not clear a refusal or an approval."""
    from cauce import grants, stops

    ran = []
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: ran.append(kw["task_id"]) or orchestrate.Report(
        kw["task_id"], "done", None))  # type: ignore[arg-type]
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    store = Store.open()
    refused = store.create_task("build it", status="running", source="cauce", repo="r", cwd=str(git_repo))
    stops.record(store, refused["id"], stops.Stop("permission", "refused", denied=("Bash(npm run build)",),
                                                  allow=("Bash(npm run build:*)",)))
    waiting = store.create_task("plan it", status="needs_approval", source="cauce", repo="r", cwd=str(git_repo))
    outgrew = store.create_task("big", status="running", source="cauce", repo="r", cwd=str(git_repo))
    stops.record(store, outgrew["id"], stops.Stop("turns", "outgrew"))
    store.close()

    # 1. refused, nothing granted: exit 3, the rule named, nothing ran
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 3
    err = capsys.readouterr().err
    assert "only the person can allow Bash(npm run build:*)" in err and "cauce allow 'Bash(npm run build:*)'" in err
    # 2. a rule passed by hand is the person's, as is one they kept for the repository
    assert cli.main(["resume", str(refused["id"]), "--unattended", "--allow", "Bash(npm run build:*)"]) == 0
    grants.grant("r", ["Bash(npm run build:*)"])
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 0
    # 3. a cell waiting for approval stays waiting
    assert cli.main(["resume", str(waiting["id"]), "--unattended"]) == 3
    assert "needs the person's approval" in capsys.readouterr().err
    # 4. work that only outgrew its turns goes on
    assert cli.main(["resume", str(outgrew["id"]), "--unattended"]) == 0
    assert ran == [refused["id"], refused["id"], outgrew["id"]]


def test_a_detached_resume_returns_at_once_and_its_ending_is_left_for_the_session(capsys, git_repo, monkeypatch):
    from cauce import stops

    store = Store.open()
    big = store.create_task("big", status="running", source="cauce", repo="r", cwd=str(git_repo), session_id="s")
    stops.record(store, big["id"], stops.Stop("turns", "outgrew"))
    store.close()
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)))
    assert cli.main(["resume", str(big["id"]), "--detach", "--max-turns", "150", "--allow", "Bash(ls:*)"]) == 0
    argv, kw = started[0]
    assert argv[1:] == ["-m", "cauce", "resume", str(big["id"]), "--no-report", "--max-turns", "150",
                        "--allow", "Bash(ls:*)"]
    assert kw["start_new_session"] and "CAUCE_SESSION_ID" not in kw["env"]
    assert "in the background" in capsys.readouterr().out
    # a detached resume does not take the ending its session is owed
    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: orchestrate.Report(kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    assert cli.main(["resume", str(big["id"]), "--no-report"]) == 0
    store = Store.open()
    assert store.last_event(big["id"], "reported") is None
    store.close()


def test_keep_allows_once_for_the_repository_and_never_unattended(capsys, git_repo, monkeypatch):
    """One press per program per repository: the next task there is not refused it again."""
    from cauce import grants, stops

    monkeypatch.setattr(orchestrate, "run", lambda *a, **kw: orchestrate.Report(kw["task_id"], "done", None))
    monkeypatch.setattr(orchestrate.Report, "text", lambda self: "ok")
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    store = Store.open()
    refused = store.create_task("build it", status="blocked", source="cauce", repo="r", cwd=str(git_repo))
    stops.record(store, refused["id"], stops.Stop("permission", "refused", denied=("Bash(npm test)",),
                                                  allow=("Bash(npm test:*)",)))
    other = store.create_task("other", status="blocked", source="cauce", repo="elsewhere", cwd=str(git_repo))
    stops.record(store, other["id"], stops.Stop("permission", "refused", allow=("Bash(npm test:*)",)))
    keyless = store.create_task("no repo", status="blocked", source="cauce", repo=None, cwd=str(git_repo))
    store.close()

    # 1. an unattended resume never keeps a rule, whatever it names: nothing granted, nothing run
    assert cli.main(["resume", str(refused["id"]), "--unattended", "--keep", "--allow", "Bash(npm test:*)"]) == 2
    assert "only a person passes it" in capsys.readouterr().err
    assert grants.granted("r") == ()
    # 2. --keep with no rule, or no repository to key them to, says so and keeps nothing
    assert cli.main(["resume", str(refused["id"]), "--keep"]) == 2
    assert "name them with --allow" in capsys.readouterr().err
    assert cli.main(["resume", str(keyless["id"]), "--keep", "--allow", "Bash(ls:*)", "--detach"]) == 2
    assert grants.granted("r") == () and started == []
    # 3. a person's press keeps the rule for the task's repository only, and a detached child does not keep twice
    assert cli.main(["resume", str(refused["id"]), "--keep", "--allow", " Bash(npm test:*) ", "--detach"]) == 0
    assert grants.granted("r") == ("Bash(npm test:*)",) and grants.granted("elsewhere") == ()
    assert "--keep" not in started[0]
    # 4. now an unattended resume of the next refusal in that repository goes on, elsewhere it still waits
    assert cli.main(["resume", str(refused["id"]), "--unattended"]) == 0
    assert cli.main(["resume", str(other["id"]), "--unattended"]) == 3
    # 5. keeping again adds nothing twice
    assert cli.main(["resume", str(refused["id"]), "--keep", "--allow", "Bash(npm test:*)"]) == 0
    assert grants.granted("r") == ("Bash(npm test:*)",)


def test_endings_are_peeked_or_delivered_once_and_only_to_their_session(capsys):
    store = Store.open()
    mine = store.create_task("write docs", status="running", source="cauce", session_id="s1", final_cell="haiku")
    store.add_event(mine["id"], "finished", status="done", final_cell="haiku")
    store.update_task(mine["id"], status="done", result="wrote them")
    other = store.create_task("other session's", status="running", source="cauce", session_id="s2")
    store.add_event(other["id"], "finished", status="failed")
    store.update_task(other["id"], status="failed")
    store.create_task("still running", status="running", source="cauce", session_id="s1")
    store.close()

    assert cli.main(["endings", "--session", "s1", "--peek", "--json"]) == 0
    peeked = json.loads(capsys.readouterr().out)
    assert peeked["ids"] == [mine["id"]] and "#1 [done] write docs" in peeked["notice"]
    assert "still running" not in peeked["notice"] and "other session" not in peeked["notice"]
    assert cli.main(["endings", "--session", "s1", "--json"]) == 0  # a peek took nothing
    assert json.loads(capsys.readouterr().out)["ids"] == [mine["id"]]
    assert cli.main(["endings", "--session", "s1", "--json"]) == 0  # delivered once
    assert json.loads(capsys.readouterr().out) == {"notice": None, "ids": []}
    assert cli.main(["endings", "--session", "nobody"]) == 0 and capsys.readouterr().out == ""
    store = Store.open()
    assert store.last_event(mine["id"], "reported")["data"]["via"] == "mod"
    store.close()


def test_note_and_topics_answer_in_json(git_repo, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["note", "Invoices are numbered per country", "--topic", "business", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": 1, "new": True, "topic": "business",
                                                   "title": "Invoices are numbered per country"}
    assert cli.main(["notes", "topics", "--json"]) == 0
    topics = json.loads(capsys.readouterr().out)
    assert topics[0] == {"name": "business", "description": topics[0]["description"], "notes": 1}
    assert [t["name"] for t in topics] == ["business", "code", "decisions", "conventions", "environment"]


def test_memory_forget_takes_a_problem_and_its_fixes_out(capsys):
    store = Store.open()
    problem = store.open_problem("Cannot find module vite", repo="r")
    store.add_fix(problem, "reinstalled node_modules", "worked", repo="r")
    store.close()
    assert cli.main(["memory", "forget", str(problem)]) == 0
    assert f"forgot problem #{problem} and its 1 fix(es)" in capsys.readouterr().out
    assert cli.main(["memory", "forget", str(problem)]) == 1
    store = Store.open()
    assert store.problem(problem) is None
    store.close()


def test_a_start_cell_with_no_such_dial_is_a_usage_error(capsys):
    import pytest

    with pytest.raises(SystemExit) as exit_:
        cli.main(["run", "--start", "haiku/turbo", "x"])
    assert exit_.value.code == 2
    assert "haiku needs an effort" in capsys.readouterr().err


def test_a_kind_alias_is_read_as_the_kind_it_means(capsys):
    from cauce import cli

    assert cli._kind("research") == "explore"
    assert "read as 'explore'" in capsys.readouterr().err
    assert cli._kind("Explore") == "explore"


def test_an_unknown_kind_names_the_kinds():
    import argparse

    import pytest

    from cauce import cli

    with pytest.raises(argparse.ArgumentTypeError, match="leave --kind out"):
        cli._kind("whatever")
