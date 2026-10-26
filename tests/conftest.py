from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cauce import workflows
from cauce.store import Store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own cauce home. Never bypass this: a test writing to
    the real database would put fake dead ends in front of real sessions."""
    home = tmp_path / "cauce-home"
    monkeypatch.setenv("CAUCE_HOME", str(home))
    monkeypatch.delenv("CAUCE_HOOKS_OFF", raising=False)
    # livespec is on by default in real use. Here it is off unless a test turns
    # it on with a fake: a real `uvx livespec index` in a unit test is minutes
    # of network and indexing nobody asked for.
    monkeypatch.setenv("CAUCE_LIVESPEC", "off")
    # The same for the two settings that would call a model or start a process:
    # a test that wants them turns them on with a fake.
    monkeypatch.setenv("CAUCE_PARALLEL", "off")
    monkeypatch.setenv("CAUCE_AUTOWORK", "off")
    monkeypatch.setenv("CAUCE_NAMES", "off")
    monkeypatch.setenv("CAUCE_NOTES", "off")
    # An independent verifier is one more worker per pass: a test that wants it turns it on.
    monkeypatch.setenv("CAUCE_VERIFIER", "off")
    # A race is a second worker on a first try: a test that wants one turns it on.
    monkeypatch.setenv("CAUCE_RACE", "off")
    monkeypatch.setenv("CAUCE_ROUTE", "off")
    monkeypatch.setenv("CAUCE_PRIMER", "off")
    monkeypatch.setenv("CAUCE_TRUNK", "off")
    # The verifier beside the check runs it on a thread: a test that wants that turns it on.
    monkeypatch.setenv("CAUCE_OVERLAP", "off")
    # Lean workers read the CLI's `--help`: a test that wants them passes a fake one.
    monkeypatch.setenv("CAUCE_LEAN", "off")
    # A role's revision is a Haiku call and a trial: a test that wants them turns them on with a fake `ask`.
    monkeypatch.setenv("CAUCE_ROLE_TRIALS", "off")
    monkeypatch.setenv("CAUCE_HABIT_REVIEW", "off")
    monkeypatch.setenv("CAUCE_HABIT_AUTO", "off")
    monkeypatch.setenv("CAUCE_ENGRAM", "off")
    monkeypatch.setenv("CAUCE_ENGRAM_WRITE", "off")
    monkeypatch.delenv("CAUCE_SESSION_ID", raising=False)
    # cauce's own suite may run inside a cauce worker, which refuses to start work.
    monkeypatch.delenv("CAUCE_WORKER_TASK", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    # The dispatcher's level starts at what the machine holds: in a test, a fixed machine
    # of three CPUs whose memory, disk and load say nothing (calm), whatever runs the suite.
    from cauce import capacity

    monkeypatch.setattr(capacity, "machine",
                        lambda root=None: capacity.Machine(cpus=3, memory_mb=None, disk_mb=None, load=None))
    monkeypatch.delenv("CAUCE_MAX_PARALLEL", raising=False)
    for var, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
    }.items():
        monkeypatch.setenv(var, value)
    return home


@pytest.fixture
def store(_isolated_home: Path) -> Store:
    s = Store(_isolated_home / "cauce.db")
    yield s
    s.close()


@pytest.fixture
def kicks(monkeypatch):
    """No real dispatcher: each start the engine asks for is recorded."""
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: seen.append((cwd, key)))
    return seen


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo
