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
