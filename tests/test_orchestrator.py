from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from locks import cli, hooks, launch, orchestrator, project, stops
from locks.store import Store


def _old(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _project(git_repo: Path) -> Path:
    return project.enroll(git_repo)


def test_the_first_session_that_sends_work_orchestrates(store: Store, git_repo: Path):
    folder = _project(git_repo)
    assert orchestrator.held(folder) is None
    assert orchestrator.sent(store, git_repo, {}) == (False, 0)  # a terminal names no one
    assert orchestrator.sent(store, git_repo, {"LOCKS_SESSION_ID": "w", "LOCKS_WORKER_TASK": "3"}) == (False, 0)
    store.touch_session("a", str(git_repo), "r")
    assert orchestrator.sent(store, git_repo, {"LOCKS_SESSION_ID": "a"}) == (True, 0)
    assert orchestrator.held(folder)["session"] == "a" and orchestrator.is_it(folder, "a")
    # Another live session sending work does not take it.
    store.touch_session("b", str(git_repo), "r")
    assert orchestrator.sent(store, git_repo, {"LOCKS_SESSION_ID": "b"}) == (False, 0)
    assert orchestrator.held(folder)["session"] == "a"


def test_a_quiet_orchestrator_hands_over_with_its_work(store: Store, git_repo: Path):
    folder = _project(git_repo)
    store._conn.execute("INSERT INTO sessions (id, cwd, repo, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                        ("a", str(git_repo), "r", _old(10), _old(8)))
    orchestrator.claim(folder, "a", str(git_repo), via="sent work")
    waiting = store.create_task("x", status="blocked", source="locks", repo="r", session_id="a")
    store.touch_session("b", str(git_repo), "r")
    assert orchestrator.sent(store, git_repo, {"LOCKS_SESSION_ID": "b"}) == (True, 1)
    assert store.get_task(waiting["id"])["session_id"] == "b"
    assert orchestrator.held(folder)["via"] == "sent work"


def test_a_clear_in_its_place_takes_the_role_and_resume_keeps_it(store: Store, git_repo: Path):
    folder = _project(git_repo)
    orchestrator.claim(folder, "a", str(git_repo), via="sent work")
    store.create_task("x", status="running", source="queue", repo="r", session_id="a")
    assert orchestrator.started(store, folder, "c", str(git_repo)) == (False, 0)  # a live one keeps it
    assert orchestrator.ended(folder, "zz", "clear") is False and orchestrator.ended(None, "a", "clear") is False
    assert orchestrator.ended(folder, "a", "clear")
    # A session elsewhere does not take it on start; the one in its place does.
    assert orchestrator.started(store, folder, "c", "/elsewhere") == (False, 0)
    assert orchestrator.started(store, folder, "c", str(git_repo)) == (True, 1)
    found = orchestrator.held(folder)
    assert found["session"] == "c" and found["via"] == "after clear" and "ended" not in found
    orchestrator.ended(folder, "c", "other")
    assert orchestrator.started(store, folder, "c", str(git_repo)) == (True, 0)  # resumed
    assert "ended" not in orchestrator.held(folder)


def test_the_orchestrator_adopts_what_quiet_sessions_left(store: Store, git_repo: Path):
    store._conn.execute("INSERT INTO sessions (id, cwd, repo, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                        ("old", "/x", "r", _old(30), _old(20)))
    left = store.create_task("x", status="blocked", source="locks", repo="r", session_id="old")
    store.touch_session("live", "/x", "r")
    kept = store.create_task("y", status="blocked", source="locks", repo="r", session_id="live")
    elsewhere = store.create_task("z", status="blocked", source="locks", repo="other", session_id="old")
    here = store.create_run(name="feature", scope="bundled", source="x", definition="{}", repo="r",
                            session_id="old", status="active")
    there = store.create_run(name="feature", scope="bundled", source="x", definition="{}", repo="other",
                             session_id="old", status="active")
    assert orchestrator.adopt(store, ["r"], "me") == 1
    assert store.get_run(here["id"])["session_id"] == "me" and store.get_run(there["id"])["session_id"] == "old"
    assert store.get_task(left["id"])["session_id"] == "me"
    assert store.get_task(kept["id"])["session_id"] == "live"
    assert store.get_task(elsewhere["id"])["session_id"] == "old"
    assert store.quiet_senders([], _old(1), but="me") == []


def test_claim_release_and_stale():
    assert orchestrator.stale(None) and orchestrator.stale("not a date") and orchestrator.stale(_old(7))
    assert not orchestrator.stale(_old(1))
    assert orchestrator.held(None) is None and not orchestrator.is_it(None, "a")


def test_release_only_by_its_holder(git_repo: Path):
    folder = _project(git_repo)
    assert orchestrator.release(folder) is False
    orchestrator.claim(folder, "a", None, via="claimed")
    assert orchestrator.release(folder, "b") is False
    assert orchestrator.release(folder, "a") is True and orchestrator.held(folder) is None
    (folder / orchestrator.FILE).write_text("[1]")
    assert orchestrator.held(folder) is None


def test_take_needs_a_project_and_a_session(store: Store):
    assert orchestrator.take(store, None, "a", None, via="x") == (False, 0)
    assert orchestrator.started(store, None, "a", None) == (False, 0)


def test_the_cli_shows_claims_and_releases(capsys, monkeypatch, store: Store, git_repo: Path):
    assert cli.main(["orchestrator", "--repo", str(git_repo)]) == 1  # no project yet
    folder = _project(git_repo)
    assert cli.main(["orchestrator", "--repo", str(git_repo)]) == 0
    assert "no session orchestrates" in capsys.readouterr().out
    assert cli.main(["orchestrator", "--repo", str(git_repo), "--claim"]) == 1  # needs the session
    orchestrator.claim(folder, "a", str(git_repo), via="sent work")
    store.create_task("x", status="blocked", source="locks", repo="r", session_id="a")
    monkeypatch.setenv("LOCKS_SESSION_ID", "b")
    capsys.readouterr()
    assert cli.main(["orchestrator", "--repo", str(git_repo), "--claim"]) == 0
    out = capsys.readouterr().out
    assert "b (this session)" in out and "1 task(s)" in out
    orchestrator.ended(folder, "b", "clear")
    assert cli.main(["orchestrator", "--repo", str(git_repo), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["this_session"] and shown["orchestrator"]["end_reason"] == "clear"
    assert cli.main(["orchestrator", "--repo", str(git_repo)]) == 0
    assert "ended (clear)" in capsys.readouterr().out
    monkeypatch.setenv("LOCKS_SESSION_ID", "z")
    assert cli.main(["orchestrator", "--repo", str(git_repo), "--release"]) == 1
    monkeypatch.setenv("LOCKS_SESSION_ID", "b")
    assert cli.main(["orchestrator", "--repo", str(git_repo), "--release"]) == 0


def test_hooks_hand_the_role_on_and_tell_the_orchestrator(store: Store, git_repo: Path, tmp_path: Path):
    folder = _project(git_repo)
    orchestrator.claim(folder, "a", str(git_repo), via="sent work")
    hooks.handle("SessionEnd", {"session_id": "a", "cwd": str(git_repo), "reason": "clear"}, store, tmp_path, {})
    assert orchestrator.held(folder)["ended"]
    said = hooks._orchestrates(store, "c", str(git_repo))
    assert said and "orchestrates this project" in said and orchestrator.is_it(folder, "c")
    assert hooks._orchestrates(store, "d", str(git_repo)) is None
    assert hooks._adopt(store, "d", str(git_repo)) == 0 and hooks._adopt(store, "c", None) == 0


def test_a_stop_waits_on_the_person_only_for_what_they_clear(store: Store):
    def ended(cause: str, **kw) -> dict:
        task = store.create_task(cause, status=stops.Stop(cause, "r").status, source="locks")
        stops.record(store, task["id"], stops.Stop(cause, "r", **kw))
        return store.get_task(task["id"])

    assert stops.waits_on(store, ended("permission", denied=("Bash(npm ci)",))) == "person"
    assert stops.waits_on(store, ended("approval", next_cell="fable/high")) == "person"
    assert stops.waits_on(store, ended("review")) == "orchestrator"
    assert stops.waits_on(store, ended("exhausted")) == "orchestrator"
    many = ended("attempts")
    for n in range(stops.UNATTENDED_ATTEMPTS):
        store.add_attempt(many["id"], seq=n + 1, cell="sonnet/high", max_turns=30, passed=False)
    assert stops.waits_on(store, many) == "person"
    assert stops.for_person(None)


def test_the_unattended_limit_is_two_runs(store: Store):
    from locks import orchestrate

    assert stops.UNATTENDED_ATTEMPTS == 2 * orchestrate.Options().max_attempts == cli.UNATTENDED_ATTEMPTS


def test_a_session_dismisses_as_the_orchestrator(capsys, monkeypatch, store: Store):
    task = store.create_task("x", status="failed", source="locks")
    monkeypatch.setenv("LOCKS_SESSION_ID", "a")
    assert cli.main(["dismiss", str(task["id"])]) == 0
    stop = stops.of(store, store.get_task(task["id"]))
    assert stop.who == "orchestrator" and "orchestrating session" in stop.reason
    assert stops.what_to_do(stop).startswith("nothing: the orchestrating session")


def test_a_worker_never_inherits_the_hosts_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "the-person")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "the-person")
    assert not set(launch.HOST_SESSION_VARS) & set(launch.worker_env())
