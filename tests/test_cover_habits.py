"""Branches of `cauce.habits` the habits suite leaves unread."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

from cauce import habits
from cauce.store import Store

from .test_habits import _events, _git_repo


def _habit(program: str, suffix: str = ".py") -> habits.Candidate:
    return habits.Candidate((f"edit:{suffix}", f"bash:{program}"), 3, 2, 1.0, 1, 6.0)


def test_an_absolute_path_outside_the_tool_folders_is_a_repository_and_no_cwd_has_no_checkout():
    assert habits._project("/srv/shop") == "/srv/shop"
    assert habits.checkout_of(None) is None
    assert habits.checkout_of("") is None


def test_a_review_another_process_queued_meanwhile_is_not_queued_again(store: Store, tmp_path: Path, monkeypatch):
    checkout = tmp_path / "py"
    (checkout / ".git").mkdir(parents=True)
    found = [_habit("pytest")]
    monkeypatch.setattr(habits, "unreviewed", lambda store, repo, top: found)
    store.set_meta(habits.REVIEWED_PREFIX + "py", json.dumps([found[0].id]))
    assert habits.queue_review(store, repo="py", cwd=str(checkout), session_id="s0", kick=lambda d, k: None) is None
    assert store.list_tasks(source=("queue",)) == []


def test_a_command_too_long_or_that_does_not_parse_is_refused():
    assert habits.shaped("pytest " + "x" * habits.REVIEW_COMMAND_CHARS, _habit("pytest")) == (
        f"it is over {habits.REVIEW_COMMAND_CHARS} characters")
    assert habits.shaped('pytest "never closed', _habit("pytest")) == "it does not parse as a command"


def test_vetting_needs_a_tracked_file_of_the_type_a_bounded_run_and_a_checkout(tmp_path: Path, monkeypatch):
    repo_dir = _git_repo(tmp_path / "r")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rlint").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "slowlint").write_text("#!/bin/sh\nsleep 5\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    why, seconds = habits.vet("rlint", _habit("rlint", ".rs"), repo_dir, tmp_path / "home")
    assert (why, seconds) == ("no tracked .rs file to try it on", None)
    monkeypatch.setattr(habits, "REVIEW_SECONDS", 0.3)
    why, seconds = habits.vet("slowlint", _habit("slowlint"), repo_dir, tmp_path / "home")
    assert why == "it ran past 0.3 s on a.py" and seconds == 0.3
    plain = tmp_path / "plain"
    plain.mkdir()
    why, seconds = habits.vet("rlint", _habit("rlint"), plain, tmp_path / "home")
    assert why.startswith("it could not be tried: ") and seconds is None


def test_a_review_applied_by_another_process_meanwhile_is_applied_once(store: Store, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAUCE_HABIT_AUTO", "on")
    task = store.create_task("r", status="done", source="queue", repo="r", cwd=str(tmp_path),
                             options=json.dumps({"habit_review": ["abcd1234"]}))
    real = store.immediate

    @contextlib.contextmanager
    def raced():
        store.set_meta(f"habits.applied:{task['id']}", "1")  # the other process got there first
        with real():
            yield

    monkeypatch.setattr(store, "immediate", raced)
    assert habits.apply_reviews(store, tmp_path / "home") == []
    assert store.events_of(task["id"], "habit_installed") == store.events_of(task["id"], "habit_refused") == []


def test_no_repository_has_no_current_habits(store: Store):
    assert habits.current(store, None, None) == []


def test_recipes_name_a_longer_sequence_once_and_stop_at_their_limit(store: Store):
    for i in range(3):
        t = store.create_task(f"t{i}", status="done", source="cauce", kind="implement", repo="r")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1)
        _events(store, 1, ["edit:.py", "bash:ruff", "bash:pytest"], task=t["id"], attempt=1)
    # Tied counts: the longest is found first and the pairs inside it are no recipe of their own,
    # whatever this process's string hashing (it was a set, read in hash order).
    assert habits.recipes(store, "implement", repo="r") == ["edit:.py → bash:ruff → bash:pytest"]
    for i in range(3):
        t = store.create_task(f"u{i}", status="done", source="cauce", kind="implement", repo="r")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1)
        _events(store, 1, ["edit:.ts", "bash:deno-fmt"], task=t["id"], attempt=1)
    assert habits.recipes(store, "implement", repo="r", limit=1) == ["edit:.py → bash:ruff → bash:pytest"]
    assert len(habits.recipes(store, "implement", repo="r", limit=5)) == 2


def test_recipes_are_the_same_in_every_process(tmp_path: Path):
    script = (
        "from pathlib import Path\nfrom cauce import habits\nfrom cauce.store import Store\n"
        f"s = Store(Path({str(tmp_path / 'db.sqlite')!r}))\n"
        "for i in range(3):\n"
        "    t = s.create_task(f't{i}', status='done', source='cauce', kind='implement', repo='r')\n"
        "    s.add_attempt(t['id'], cell='sonnet/medium', max_turns=30, passed=1)\n"
        "    for sig in ('edit:.py', 'bash:ruff', 'bash:pytest', 'bash:mypy'):\n"
        "        s.add_tool_event(session_id='x', task_id=t['id'], attempt=1, tool='x', sig=sig, arg_hash='h', ok=1)\n"
        "print(habits.recipes(s, 'implement', repo='r', limit=5))\n")
    seen = set()
    for seed in ("1", "2", "3", "4"):
        out = subprocess.run([os.sys.executable, "-c", script], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONHASHSEED": seed, "CAUCE_HOME": str(tmp_path / "h")}).stdout
        (tmp_path / "db.sqlite").unlink()
        seen.add(out)
    assert len(seen) == 1, seen


def test_a_habit_that_hangs_or_cannot_start_counts_as_a_failure(store: Store, tmp_path: Path, monkeypatch):
    hid, _ = habits.install(store, _habit("pytest"), command="pytest -q", repo_dir=tmp_path)
    event = {"tool_input": {"file_path": "/r/a.py"}, "cwd": str(tmp_path)}

    def hangs(*a, **kw):
        raise subprocess.TimeoutExpired("pytest -q", 110)

    monkeypatch.setattr(habits.subprocess, "run", hangs)
    assert "it ran past 110 seconds and was stopped" in habits.run_habit(store, hid, event)

    def missing(*a, **kw):
        raise OSError("no such program: pytest")

    monkeypatch.setattr(habits.subprocess, "run", missing)
    assert "no such program: pytest" in habits.run_habit(store, hid, event)
    assert store.habit(hid)["failures"] == 2
