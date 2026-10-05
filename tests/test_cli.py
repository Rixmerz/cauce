from __future__ import annotations

import json

from cauce import cli, orchestrate
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
    assert code == 1 and "task #1: failed" in capsys.readouterr().out


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

    assert cli.main(["memory", "record", "--problem", "slow startup", "--fix", "lazy import pandas",
                     "--outcome", "failed", "--why", "still slow", "--repo", str(git_repo)]) == 0
    assert cli.main(["memory", "search", "slow startup", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "lazy import pandas" in out and "still slow" in out
    assert cli.main(["memory", "search", "nothing like it"]) == 0
    assert "nothing recorded" in capsys.readouterr().out


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
    assert json.loads(capsys.readouterr().out) == {"needs_you": 1, "running": 0, "workers": 0, "queued": 1, "done": 0}
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
    assert board["counts"] == {"needs_you": 0, "running": 0, "workers": 0, "queued": 1, "done": 0}
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

    assert cli.main(["projects", "--json"]) == 0
    projects = {x["repo"]: x for x in json.loads(capsys.readouterr().out)}
    assert projects[key]["dir"] == str(git_repo.resolve()) and projects[key]["exists"]
    assert projects[key]["sessions"] == 1 and projects[key]["tasks"] == {"queued": 1}
    assert projects["github.com/o/other"]["exists"] is False
    assert cli.main(["projects"]) == 0 and "(gone)" in capsys.readouterr().out

    assert cli.main(["sessions", "--json", "--repo", str(git_repo)]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert [x["id"] for x in sessions] == ["s-1"]
    assert sessions[0]["prompts"] == 1 and sessions[0]["last_prompt"] == "fix the cart total"
    assert sessions[0]["resume"].endswith("&& claude --resume s-1")
    assert cli.main(["sessions"]) == 0 and "claude --resume s-2" in capsys.readouterr().out

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
