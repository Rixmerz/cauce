"""Merging the branches tasks kept, on the person's word (`cauce merge`)."""
from __future__ import annotations

from cauce import cli, merging
from cauce.store import Store

from .conftest import git


def _kept(store: Store, repo, text: str, files: dict[str, str]) -> dict:
    task = store.create_task(text, status="done", source="cauce", repo="r", cwd=str(repo))
    git(repo, "checkout", "-q", "-b", f"cauce/task-{task['id']}")
    for name, body in files.items():
        (repo / name).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", text)
    git(repo, "checkout", "-q", "main")
    store.add_event(task["id"], "finished", status="done", changed=sorted(files), branch=f"cauce/task-{task['id']}")
    return task


def test_the_branches_that_touch_no_other_go_first():
    assert merging.order({3: ["a.ts", "b.ts"], 4: ["c.ts"], 5: ["b.ts"]}) == [4, 3, 5]


def test_merge_takes_each_branch_in_order_and_stops_at_a_conflict(git_repo, capsys):
    store = Store.open()
    try:
        alone = _kept(store, git_repo, "add the forum", {"forum.ts": "forum\n"})
        first = _kept(store, git_repo, "add the API service", {"api.spec.ts": "first\n"})
        clash = _kept(store, git_repo, "add the admin", {"api.spec.ts": "second\n", "admin.ts": "a\n"})
    finally:
        store.close()
    assert cli.main(["merge", str(first["id"]), str(alone["id"]), str(clash["id"]), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith(f"would merge, in this order: #{alone['id']}")
    assert cli.main(["merge", str(first["id"]), str(alone["id"]), str(clash["id"]), "--verify", "true"]) == 1
    out = capsys.readouterr()
    assert f"merged #{alone['id']}" in out.out and "`true` passes after it" in out.out
    assert f"#{clash['id']} (cauce/task-{clash['id']}) conflicts with what is in main in api.spec.ts" in out.err
    assert "cauce run 'Merge branch cauce/task-" in out.err and f"not merged: #{clash['id']}" in out.out
    assert (git_repo / "forum.ts").exists() and (git_repo / "api.spec.ts").read_text() == "first\n"
    assert git(git_repo, "status", "--porcelain").strip() == ""  # the conflicting merge was aborted


def test_merge_refuses_a_dirty_checkout_and_stops_where_the_check_fails(git_repo, capsys):
    store = Store.open()
    try:
        one = _kept(store, git_repo, "add a", {"a.ts": "a\n"})
    finally:
        store.close()
    (git_repo / "README.md").write_text("changed\n")
    git(git_repo, "add", "README.md")
    assert cli.main(["merge", str(one["id"])]) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    git(git_repo, "reset", "-q", "--hard")
    assert cli.main(["merge", str(one["id"]), "--verify", "false"]) == 1
    assert "`false` fails after merging" in capsys.readouterr().err
    assert cli.main(["merge", "999"]) == 1


def test_a_pull_request_carries_the_verdict_and_opens_only_through_gh(git_repo, capsys, monkeypatch):
    import subprocess as sp

    store = Store.open()
    try:
        task = _kept(store, git_repo, "add the export", {"export.py": "x\n"})
        store.update_task(task["id"], result="Added the CSV export.")
        store.add_event(task["id"], "finished", status="done", changed=["export.py"],
                        verdict={"check": {"command": "pytest -q"},
                                 "criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"}]})
    finally:
        store.close()
    assert cli.main(["pr", str(task["id"]), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("title: add the export") and "Added the CSV export." in out
    assert "- check: `pytest -q` passed, run by cauce in its worktree" in out and "- `export.py`" in out
    assert "do not re-read" not in out
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, "https://example.test/pr/1\n", "")

    opened, said = merging.open_pr(git_repo, f"cauce/task-{task['id']}", "t", "b", run=run)
    assert opened and said == "https://example.test/pr/1"
    assert calls[0][:3] == ["git", "push", "-u"] and calls[1][:4] == ["gh", "pr", "create", "--draft"]
    refused = merging.open_pr(git_repo, "x", "t", "b", run=lambda argv, **kw: sp.CompletedProcess(argv, 1, "", "no"))
    assert refused == (False, "git push failed: no")
    assert cli.main(["pr", "999"]) == 1
