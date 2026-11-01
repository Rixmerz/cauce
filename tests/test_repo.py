from __future__ import annotations

import pytest

from cauce import repo

from .conftest import git


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Rixmerz/cauce.git",
        "https://github.com/Rixmerz/cauce",
        "https://github.com/Rixmerz/cauce.git/",
        "ssh://git@github.com/Rixmerz/cauce.git",
        "https://user:token@GitHub.com:443/Rixmerz/cauce.git",
    ],
)
def test_one_repository_one_key(url):
    assert repo.normalize_remote(url) == "github.com/Rixmerz/cauce"


def test_unparseable_remote_is_kept():
    assert repo.normalize_remote("/srv/git/project") == "/srv/git/project"


def test_key_prefers_origin_then_toplevel(git_repo, tmp_path):
    assert repo.key(git_repo) == str(git_repo.resolve())
    sub = git_repo / "pkg"
    sub.mkdir()
    assert repo.key(sub) == str(git_repo.resolve())
    git(git_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    assert repo.key(sub) == "github.com/o/r"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo.key(plain) == str(plain.resolve())
    assert repo.key(tmp_path / "missing") is None
    assert repo.key(None) is None
    assert repo.toplevel(plain) is None


def test_what_git_said_of_a_folder_is_kept_until_a_stat_shows_it_changed(git_repo, monkeypatch):
    """`toplevel` and a checkout's key are asked of git once per process (a hook asked a dozen times
    per folder), and asked again when a repository is made inside, or the remote changes."""
    import subprocess

    from cauce import repo

    calls = []
    real = repo._git
    monkeypatch.setattr(repo, "_git", lambda d, *a: calls.append(a) or real(d, *a))
    inner = git_repo / "pkg"
    inner.mkdir()
    assert repo.toplevel(inner) == git_repo.resolve() and repo.toplevel(inner) == git_repo.resolve()
    assert repo.key(git_repo) == repo.key(git_repo) == str(git_repo.resolve())
    assert len(calls) == 3  # a rev-parse for `pkg`, one for the checkout and one remote lookup, each once
    subprocess.run(["git", "init", "-q", str(inner)], check=True)
    assert repo.toplevel(inner) == inner.resolve()
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:o/r.git"], cwd=git_repo, check=True)
    assert repo.key(git_repo) == "github.com/o/r"
