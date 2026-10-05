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
