"""Branches of `locks.survey` the survey suite leaves unread."""
from __future__ import annotations

import subprocess
from pathlib import Path

from locks import repo, survey

from .conftest import git
from .test_survey import assistant, result, session, tool, user, write


def test_a_tool_error_outside_the_shell_and_a_write_without_a_path(tmp_path: Path, git_repo: Path):
    path = write(tmp_path / "claude", "p", "s1", [
        user("look at the cart", git_repo),
        assistant([tool("r1", "Read", file_path="/x/cart.py"), tool("w1", "Write", file_path=3)]),
        result("r1", "EACCES: permission denied, open /x/cart.py", error=True),
    ])
    facts = survey.read_session(path)
    assert facts["tool_errors"] == {"Read": 1}
    assert facts["written"] == 0 and facts["unsolved"] == []


def test_a_session_s_shape_names_a_kind_once_however_often_in_a_row():
    facts = {"kinds": ["implement", "implement", "test", "implement"], "plan_mode": 0, "commits": 0, "prs": 0,
             "pushes": 0, "checks": 0, "interrupts": 0, "corrections": [], "requests": ["a"]}
    assert survey.shape(facts).startswith("implement → test → implement")


def test_git_that_cannot_run_reads_as_nothing(tmp_path: Path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert survey._git(tmp_path, "log") is None
    assert survey._landed(tmp_path, "main", "feature") is False  # git named no file
    monkeypatch.setattr(survey, "_git", lambda top, *a, **kw: "cart.py\n")
    assert survey._landed(tmp_path, "main", "feature") is False  # the diff could not run


def test_a_log_record_cut_short_is_skipped_and_an_empty_log_has_no_commits(tmp_path: Path, monkeypatch):
    whole = "\x1eabc\x1fabcdef\x1f2026-10-01\x1ffix: x\x1fbody\x1d\ncart.py\n"
    monkeypatch.setattr(survey, "_git", lambda top, *a, **kw: "\x1eshort\x1fonly\x1d\n" + whole)
    (commit,) = survey._commits(tmp_path, limit=5)
    assert (commit["sha"], commit["subject"], commit["files"]) == ("abc", "fix: x", ["cart.py"])
    monkeypatch.setattr(survey, "_git", lambda top, *a, **kw: "")
    assert survey.history(tmp_path) == {"commits": 0}


def test_a_remote_copy_of_a_local_branch_at_the_same_commit_is_one_branch(git_repo: Path):
    git(git_repo, "branch", "feat-x")
    (git_repo / "x.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "feat: x")
    git(git_repo, "branch", "-f", "feat-x", "HEAD")
    git(git_repo, "reset", "-q", "--hard", "HEAD~1")
    git(git_repo, "update-ref", "refs/remotes/origin/feat-x", "feat-x")
    found = survey.branches(git_repo)
    assert [b["branch"] for b in found["listed"]] == ["feat-x"] and found["count"] == 1


def test_a_transcript_that_cannot_be_read_is_left_out_and_counted(tmp_path: Path, store, git_repo: Path,
                                                                  monkeypatch):
    claude = tmp_path / "claude-config"
    write(claude, "p", "good", session(git_repo))
    (claude / "projects" / "p" / "gone.jsonl").symlink_to(tmp_path / "nowhere")
    (claude / "projects" / "p" / "folder.jsonl").mkdir()
    paths, old = survey.transcripts(claude)
    assert sorted(p.name for p in paths) == ["folder.jsonl", "good.jsonl"] and old == 0
    found = survey.gather(store)
    assert found["read"]["skipped"]["unreadable"] == 1
    assert found["read"]["sessions"] == 1


def test_a_directory_that_cannot_be_resolved_or_whose_checkout_is_home_is_read_so(tmp_path: Path, git_repo: Path,
                                                                                  monkeypatch):
    workers = tmp_path / "workers"
    real = Path.resolve
    bad = tmp_path / "bad"

    def resolve(self, strict=False):
        if self == bad:
            raise OSError("cannot resolve")
        return real(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert survey._key(bad, workers) is None
    sub = git_repo / "src"
    sub.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: git_repo))
    assert survey._key(sub, workers) == (repo.key(sub), None)


def test_a_project_without_shapes_says_none(tmp_path: Path, store, git_repo: Path):
    write(tmp_path / "claude-config", "p", "s1", session(git_repo))
    found = survey.gather(store)
    found["projects"][0]["shapes"] = []
    text = survey.text(found)
    assert "  shapes:" not in text and "  friction:" in text
