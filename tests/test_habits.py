from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cauce import habits, signature
from cauce.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("command", "sig"),
    [
        ("pnpm test --watch", "bash:pnpm-test"),
        ('FOO=1 git commit -m "fix: x"', "bash:git-commit"),
        ("python -m pytest -q tests/a.py", "bash:pytest"),
        ("/usr/bin/git push origin main", "bash:git-push"),
        ("sudo rm -rf /tmp/x", "bash:rm"),
        ("curl https://example.com/secret?token=abc", "bash:curl"),
        ("sk-abcdef1234567890", "bash:unknown"),
        ("ruff format src && pytest", "bash:ruff-format"),
        ("cargo build --release", "bash:cargo-build"),
        ("git -C /some/path status", "bash:git"),
        ("", "bash:unknown"),
        ("echo 'unterminated", "bash:echo"),
    ],
)
def test_a_signature_names_the_verb_and_never_the_arguments(command, sig):
    assert signature.signature("Bash", {"command": command}) == sig


def test_file_mcp_and_other_tools():
    assert signature.signature("Edit", {"file_path": "/home/me/secret/app.PY"}) == "edit:.py"
    assert signature.signature("Write", {"file_path": "/x/Makefile"}) == "write:noext"
    assert signature.signature("NotebookEdit", {"notebook_path": "/a/b.ipynb"}) == "edit:.ipynb"
    mcp = signature.signature("mcp__plugin_livespec_livespec__who_calls", {})
    assert mcp == "mcp:plugin_livespec_livespec.who_calls"
    assert signature.signature("Grep", None) == "grep"
    assert signature.signature("", None) == "unknown"
    assert signature.arg_hash({"b": 1, "a": 2}) == signature.arg_hash({"a": 2, "b": 1})


def test_the_log_line_holds_no_argument(tmp_path):
    env = {"CAUCE_HOME": str(tmp_path), "CAUCE_WORKER_TASK": "7", "CAUCE_WORKER_ATTEMPT": "2"}
    event = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest tests/secret_name.py"},
             "tool_response": {"interrupted": True}}
    signature.append(event, env)
    raw = next((tmp_path / "tool-events").glob("*.ndjson")).read_text()
    assert "secret_name" not in raw
    record = json.loads(raw)
    assert record["sig"] == "bash:pytest" and record["task_id"] == 7 and record["attempt"] == 2
    assert record["ok"] == 0
    assert signature.log_dir({"XDG_DATA_HOME": "/x"}) == Path("/x/cauce/tool-events")
    assert signature.log_dir({}).name == "tool-events"


def _events(store: Store, sessions: int, seq: list[str], ok: bool = True, task=None, attempt=None):
    for s in range(sessions):
        for sig in seq:
            store.add_tool_event(session_id=f"s{s}", task_id=task, attempt=attempt, tool="x", sig=sig,
                                 arg_hash="h", ok=int(ok))


def test_candidates_pass_every_gate_or_none(store: Store):
    _events(store, 3, ["edit:.py", "bash:ruff-format", "bash:pytest"])
    _events(store, 1, ["edit:.go", "bash:go-test"])  # one session: below the gate
    _events(store, 3, ["edit:.ts", "bash:rm"])  # destructive: never a candidate
    _events(store, 3, ["bash:make", "bash:make"])  # a loop is not a habit
    found = habits.candidates(store.tool_events())
    assert [c.steps for c in found] == [("edit:.py", "bash:ruff-format", "bash:pytest")]
    best = found[0]
    assert best.occurrences == 3 and best.sessions == 3 and best.success == 1.0
    assert best.id in best.line() and "×3 in 3 sessions" in best.line()


def test_a_sequence_that_mostly_fails_is_no_habit(store: Store):
    _events(store, 4, ["edit:.py", "bash:pytest"], ok=False)
    assert habits.candidates(store.tool_events()) == []


def test_load_events_moves_complete_lines_once(store: Store, _isolated_home):
    env = {"CAUCE_HOME": str(_isolated_home)}
    signature.append({"session_id": "s", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}, env)
    path = next((_isolated_home / "tool-events").glob("*.ndjson"))
    with path.open("a") as fh:
        fh.write("not json\n" + '{"partial": ')
    assert habits.load_events(store, env) == 1
    assert habits.load_events(store, env) == 0
    assert habits.load_events(store, {"CAUCE_HOME": str(_isolated_home / "none")}) == 0
    assert store.tool_events()[0]["sig"] == "read:.py"


def test_recipes_come_from_passing_worker_attempts(store: Store):
    for i in range(3):
        t = store.create_task(f"t{i}", status="done", source="cauce", kind="implement", repo="r")
        store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=1)
        _events(store, 1, ["read:.py", "edit:.py", "bash:pytest", "bash:git-push"], task=t["id"], attempt=1)
    failed = store.create_task("f", status="failed", source="cauce", kind="implement", repo="r")
    store.add_attempt(failed["id"], cell="sonnet/medium", max_turns=30, passed=0)
    _events(store, 1, ["edit:.py", "bash:make"], task=failed["id"], attempt=1)
    learned = habits.recipes(store, "implement", repo="r")
    assert learned == ["edit:.py → bash:pytest"]
    assert habits.recipes(store, "docs", repo="r") == []


def test_install_run_demote_and_uninstall(store: Store, tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / ".claude").mkdir(parents=True)
    (repo_dir / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["x"]}}))
    cand = habits.Candidate(("edit:.py", "bash:ruff-format"), 3, 3, 1.0, 1, 9.0)
    hid = habits.install(store, cand, command="exit 1", repo_dir=repo_dir)
    settings = json.loads((repo_dir / ".claude" / "settings.local.json").read_text())
    assert settings["permissions"] == {"allow": ["x"]}  # what was there stays
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert command.endswith(f"habit-run {hid}") and str(habits.launcher()) in command

    event = {"tool_input": {"file_path": "/r/a.py"}, "cwd": str(tmp_path)}
    habits.run_habit(store, hid, {"tool_input": {"file_path": "/r/a.md"}})  # not its file type: no run
    assert store.habit(hid)["runs"] == 0
    for _ in range(3):
        habits.run_habit(store, hid, event)
    assert store.habit(hid)["disabled_at"] and store.habit(hid)["failures"] == 3
    habits.run_habit(store, hid, event)  # off: does nothing
    assert store.habit(hid)["runs"] == 3

    ok = habits.install(store, cand, command="true", repo_dir=repo_dir)
    habits.run_habit(store, ok, event)
    assert store.habit(ok)["runs"] == 1 and store.habit(ok)["failures"] == 0
    assert habits.uninstall(store, hid) and not habits.uninstall(store, 999)
    left = json.loads((repo_dir / ".claude" / "settings.local.json").read_text())["hooks"]["PostToolUse"]
    assert len(left) == 1 and f"habit-run {ok}" in left[0]["hooks"][0]["command"]
    assert [h["id"] for h in store.habits()] == [ok]
    habits.run_habit(store, 999, event)

    with pytest.raises(habits.HabitError):
        habits.install(store, habits.Candidate(("bash:make", "bash:pytest"), 3, 3, 1.0, 1, 9.0),
                       command="x", repo_dir=repo_dir)
    (repo_dir / ".claude" / "settings.local.json").write_text("{broken")
    with pytest.raises(habits.HabitError):
        habits.install(store, cand, command="x", repo_dir=repo_dir)


def test_the_fast_path_logs_in_its_own_process(tmp_path):
    env = {**os.environ, "CAUCE_HOME": str(tmp_path)}
    event = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "pytest"}})
    proc = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "PostToolUse"], input=event, text=True,
                          capture_output=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout == ""
    assert "bash:pytest" in next((tmp_path / "tool-events").glob("*.ndjson")).read_text()
    proc = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "PostToolUse"], input="garbage", text=True,
                          capture_output=True, env=env, check=False)
    assert proc.returncode == 0 and "PostToolUse" in (tmp_path / "hook-errors.log").read_text()
    delegation = json.dumps({"session_id": "s", "tool_name": "Agent", "tool_use_id": "x", "tool_input": {}})
    proc = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "PostToolUse"], input=delegation, text=True,
                          capture_output=True, env=env, check=False)
    assert proc.returncode == 0
    off = subprocess.run([str(ROOT / "bin" / "cauce"), "hook", "PostToolUse"], input=delegation, text=True,
                         capture_output=True, env={**env, "CAUCE_HOOKS_OFF": "1"}, check=False)
    assert off.returncode == 0
