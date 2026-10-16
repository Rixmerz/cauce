from __future__ import annotations

import json
from pathlib import Path

import pytest

from cauce import cli, isolate, launch, notes, project, repo, workspaces
from cauce.store import Store, home

from .conftest import git


def checkout(where: Path, file: str = "app.py") -> Path:
    where.mkdir(parents=True)
    git(where, "init", "-q", "-b", "main")
    (where / file).write_text("print('hi')\n")
    (where / "README.md").write_text("same in both\n")
    git(where, "add", "-A")
    git(where, "commit", "-q", "-m", "init")
    return where


def test_a_folder_of_two_repositories_is_one_project(tmp_path, store: Store, capsys):
    folder = tmp_path / "shop"
    api = checkout(folder / "api", "server.py")
    web = checkout(tmp_path / "web", "client.ts")
    before = {"api": repo.key(api), "web": repo.key(web)}
    assert before["api"] != before["web"]
    # What each repository knew on its own: the same fact twice, and a note anchored to a file.
    store.add_note(before["api"], "conventions", "Money", "Money is integer cents.", author="person",
                   filed_by="person")
    store.add_note(before["web"], "conventions", "Money", "Money is  integer cents.", author="person",
                   filed_by="person")
    anchored = store.add_note(before["web"], "code", "Client", "The client talks to the API.", author="person",
                              filed_by="person")
    notes.anchor(store, anchored, web, ["client.ts"])
    store.create_task("old work", status="done", source="cauce", repo=before["api"], cwd=str(api))
    store._conn.commit()

    assert cli.main(["workspace", "add", str(folder), "--with", str(web)]) == 0
    said = capsys.readouterr().out
    assert "one project now: 3 note(s) and 1 task(s) moved to it, 1 repeated note(s) merged" in said
    # The repository outside the folder is linked in under its own name, and every repository answers to the folder.
    assert (folder / "web").is_symlink() and (folder / "web").resolve() == web.resolve()
    key = str(folder.resolve())
    assert repo.key(api) == repo.key(web) == repo.key(folder / "web") == repo.key(folder) == key
    assert repo.own_key(api) == before["api"]
    assert project.find(web) == folder / ".cauce" and project.find(api) == folder / ".cauce"
    # One memory: the notes are the folder's, the copy merged, each anchor named with its repository in front.
    live = store.notes(key, states=notes.LIVE)
    assert sorted(n["title"] for n in live) == ["Client", "Money"]
    assert [a["path"] for a in store.note_anchors([anchored])] == ["web/client.ts"]
    assert store.list_tasks(repo=key, source=("cauce",))[0]["title"] == "old work"
    # A worker's worktree of a member belongs to the workspace too.
    ws = isolate.prepare(api, 7, home())
    assert repo.key(ws.path) == key
    # A task in the folder sees the linked repository as one of its checkouts.
    assert {p.name for p in launch.nested_checkouts(folder)} == {"api", "web"}

    assert cli.main(["workspace", "list"]) == 0
    assert "api (" in capsys.readouterr().out
    assert cli.main(["workspace", "rm", str(folder)]) == 0
    assert repo.key(api) == before["api"]
    assert cli.main(["workspace", "rm", str(folder)]) == 1


def test_a_member_reviews_only_its_own_anchors(tmp_path, store: Store):
    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    web = checkout(folder / "web")
    workspaces.add(folder)
    key = repo.key(api)
    note = store.add_note(key, "code", "Readme", "The readme says it.", author="person", filed_by="person")
    notes.anchor(store, note, api, ["README.md"])
    assert [a["path"] for a in store.note_anchors([note])] == ["api/README.md"]
    # The other member's file of the same name changed: no reason to review a note about this one's.
    (web / "README.md").write_text("changed\n")
    git(web, "commit", "-qam", "change")
    assert notes.review(store, key, web) == []
    (api / "README.md").write_text("changed too\n")
    git(api, "commit", "-qam", "change")
    assert [n for n, _ in notes.review(store, key, api)] == [note]
    notes.confirm(store, note, api)
    assert store.get_note(note)["state"] in ("current", "review")


def test_what_cannot_be_a_workspace_is_refused(tmp_path, store: Store, capsys, monkeypatch):
    lone = tmp_path / "lone"
    checkout(lone / "api")
    with pytest.raises(workspaces.WorkspaceError, match="a workspace holds 2 or more"):
        workspaces.add(lone)
    with pytest.raises(workspaces.WorkspaceError, match="never a project"):
        workspaces.add(Path.home())
    with pytest.raises(workspaces.WorkspaceError, match="is itself a checkout"):
        workspaces.add(lone / "api")
    with pytest.raises(workspaces.WorkspaceError, match="is not in a git checkout"):
        workspaces.add(lone, [tmp_path])
    other = checkout(tmp_path / "other")
    workspaces.add(lone, [other])
    second = tmp_path / "second"
    checkout(second / "x")
    with pytest.raises(workspaces.WorkspaceError, match="overlaps"):
        workspaces.add(second, [other])
    # A refused workspace left no link behind; a name already taken in the folder is said.
    assert not (second / "other").exists()
    (second / "other").mkdir()
    with pytest.raises(workspaces.WorkspaceError, match="is already there"):
        workspaces.add(second, [checkout(tmp_path / "elsewhere" / "other")])
    # Work still going in a repository holds the workspace back.
    busy = checkout(tmp_path / "busy")
    store.create_task("going", status="running", source="cauce", repo=repo.key(busy), cwd=str(busy))
    store._conn.commit()
    assert cli.main(["workspace", "add", str(tmp_path / "third"), "--with", str(busy)]) == 1
    assert "queued or running" in capsys.readouterr().err
    assert cli.main(["workspace", "add", str(tmp_path / "fourth")]) == 2


def test_an_unreadable_file_declares_nothing(tmp_path):
    home().mkdir(parents=True, exist_ok=True)
    (home() / workspaces.FILE).write_text("not json")
    assert workspaces.declared() == [] and workspaces.of(tmp_path) is None and workspaces.prefix(None) == ""
    (home() / workspaces.FILE).write_text(json.dumps({"workspaces": [{"no": "folder"}]}))
    assert workspaces.declared() == []


def test_a_task_that_spans_two_repositories_is_offered_a_workspace(tmp_path, store: Store):
    from cauce.orchestrate import Options, run

    from .test_orchestrate import kind

    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    web = checkout(folder / "web")
    dry = run("wire the client to the new endpoint", api, store, Options(dry_run=True, add_dirs=(str(web),)),
              registry={}, classifier=kind("implement"))
    offer = [r for r in dry.plan.reasons if r.startswith("this task spans")]
    assert len(offer) == 1 and f"cauce workspace add {folder}" in offer[0] and "--with" in offer[0]
    workspaces.add(folder)
    dry = run("wire the client to the new endpoint", api, store, Options(dry_run=True, add_dirs=(str(web),)),
              registry={}, classifier=kind("implement"))
    assert not [r for r in dry.plan.reasons if r.startswith("this task spans")]


def test_a_workspace_keeps_the_team_and_the_rules_its_repositories_had(tmp_path, store: Store, capsys):
    from cauce import grants, teams

    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    web = checkout(folder / "web")
    assert cli.main(["team", "create", "web", "--from", "cauce", "--description", "web"]) == 0
    teams.use(repo.key(api), "web")
    grants.grant(repo.key(web), ["Bash(npm test:*)"])
    assert cli.main(["workspace", "add", str(folder)]) == 0
    key = str(folder.resolve())
    assert teams.assignments()[key] == "web" and "Bash(npm test:*)" in grants.granted(key)
    # Two repositories that used two teams: the person picks the folder's.
    other = tmp_path / "two"
    one = checkout(other / "one")
    two = checkout(other / "two")
    assert cli.main(["team", "create", "api", "--from", "cauce", "--description", "api"]) == 0
    teams.use(repo.key(one), "web")
    teams.use(repo.key(two), "api")
    capsys.readouterr()
    assert cli.main(["workspace", "add", str(other)]) == 0
    assert "used the teams api, web" in capsys.readouterr().out and str(other.resolve()) not in teams.assignments()
