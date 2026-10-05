from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    monkeypatch.delenv("CAUCE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
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
