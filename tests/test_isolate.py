from __future__ import annotations

import pytest

from cauce import isolate

from .conftest import git


def test_a_passed_task_leaves_one_branch_and_the_checkout_untouched(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 7, tmp_path / "home")
    assert ws.branch == "cauce/task-7" and ws.path.is_dir()
    (ws.path / "feature.py").write_text("x = 1\n")
    assert isolate.has_changes(ws)
    assert isolate.finish(ws, keep=True, message="cauce task #7") == "cauce/task-7"
    assert not ws.path.exists()
    assert not (git_repo / "feature.py").exists()
    assert "feature.py" in git(git_repo, "show", "--name-only", "cauce/task-7")


def test_reset_discards_a_wrong_approach(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 8, tmp_path / "home")
    (ws.path / "app.py").write_text("broken\n")
    (ws.path / "junk.txt").write_text("junk\n")
    isolate.reset(ws)
    assert (ws.path / "app.py").read_text() == "print('hi')\n"
    assert not (ws.path / "junk.txt").exists()
    assert not isolate.has_changes(ws)


def test_a_failed_task_leaves_nothing(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 9, tmp_path / "home")
    (ws.path / "x.py").write_text("x\n")
    assert isolate.finish(ws, keep=False, message="m") is None
    assert "cauce/task-9" not in git(git_repo, "branch", "--list")


def test_a_pass_that_changed_nothing_keeps_no_branch(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 10, tmp_path / "home")
    assert isolate.finish(ws, keep=True, message="m") is None


def test_outside_git_is_an_error(tmp_path):
    with pytest.raises(isolate.IsolationError):
        isolate.prepare(tmp_path, 1, tmp_path / "home")


def _with_dependencies(repo):
    (repo / ".gitignore").write_text("node_modules/\n.claude/settings.local.json\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (repo / "server" / "node_modules").mkdir(parents=True)
    (repo / "server" / "keep.js").write_text("1\n")
    git(repo, "add", "server/keep.js")
    git(repo, "commit", "-q", "-m", "server")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text('{"permissions": {"allow": ["Bash(npm run build)"]}}')


def test_a_worktree_gets_the_checkouts_dependencies_and_local_settings_never_commits_them(git_repo, tmp_path):
    _with_dependencies(git_repo)
    ws = isolate.prepare(git_repo, 11, tmp_path / "home")
    assert ws.links == (".claude/settings.local.json", "node_modules", "server/node_modules")
    assert (ws.path / "node_modules" / "left-pad" / "index.js").read_text() == "module.exports = 1\n"
    copied = ws.path / ".claude" / "settings.local.json"
    assert not copied.is_symlink() and "npm run build" in copied.read_text()
    assert isolate.changed(ws) == () and not isolate.has_changes(ws)
    # a worker that adds everything and commits commits the links too
    (ws.path / "feature.js").write_text("x\n")
    git(ws.path, "add", "-A")
    git(ws.path, "-c", "user.name=w", "-c", "user.email=w@x", "commit", "-q", "-m", "worker")
    (ws.path / " spaced.js").write_text("y\n")
    (ws.path / "app.py").write_text("changed\n")
    assert isolate.changed(ws) == (" spaced.js", "app.py", "feature.js")
    assert isolate.finish(ws, keep=True, message="m") == "cauce/task-11"
    files = git(git_repo, "ls-tree", "-r", "--name-only", "cauce/task-11").split()
    assert "feature.js" in files and "app.py" in files
    assert not any("node_modules" in f or "settings.local" in f for f in files)
    assert (git_repo / "node_modules" / "left-pad" / "index.js").exists()
    assert (git_repo / ".claude" / "settings.local.json").exists()


def test_reset_puts_the_links_back(git_repo, tmp_path):
    _with_dependencies(git_repo)
    ws = isolate.prepare(git_repo, 12, tmp_path / "home")
    (ws.path / "node_modules").unlink()
    isolate.reset(ws)
    assert (ws.path / "node_modules").is_symlink()
    isolate.finish(ws, keep=False, message="m")
    assert (git_repo / "node_modules" / "left-pad").is_dir()


def test_a_kept_branch_is_where_a_resumed_task_continues(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 13, tmp_path / "home")
    assert isolate.kept(git_repo, 13)  # the branch exists while it runs
    (ws.path / "half.py").write_text("half\n")
    assert isolate.finish(ws, keep=True, message="cauce task #13 (unverified, blocked)") == "cauce/task-13"
    assert isolate.kept(git_repo, 13)
    again = isolate.prepare(git_repo, 13, tmp_path / "home")
    assert (again.path / "half.py").read_text() == "half\n"
    assert again.base == git(git_repo, "rev-parse", "HEAD")
    assert isolate.has_changes(again)  # the kept work alone keeps the branch
    assert isolate.finish(again, keep=True, message="m") == "cauce/task-13"


def test_a_run_in_a_folder_holds_the_checkouts_inside_it(tmp_path):
    from cauce import isolate

    (tmp_path / "api" / ".git").mkdir(parents=True)
    (tmp_path / "web" / ".git").mkdir(parents=True)
    (tmp_path / "notes").mkdir()
    folder = isolate.held_by(tmp_path, None)
    assert folder == sorted([tmp_path.resolve(), (tmp_path / "api").resolve(), (tmp_path / "web").resolve()])
    assert isolate.held_by(tmp_path / "api", tmp_path / "api") == [(tmp_path / "api").resolve()]
    assert isolate.held_by(tmp_path / "gone", None) == [(tmp_path / "gone").resolve()]
