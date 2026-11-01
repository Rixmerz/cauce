from __future__ import annotations

import json
from pathlib import Path

import pytest

from locks import cli, isolate, launch, notes, project, repo, workspaces
from locks.store import Store, home

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
    store.create_task("old work", status="done", source="locks", repo=before["api"], cwd=str(api))
    store._conn.commit()

    # Naming some repositories of a folder that holds others is refused: every one under it would belong.
    assert cli.main(["workspace", "add", str(folder), "--with", str(web)]) == 2
    assert "also holds api" in capsys.readouterr().err and not (folder / "web").exists()
    assert cli.main(["workspace", "add", str(folder), "--with", str(api), "--with", str(web)]) == 0
    said = capsys.readouterr().out
    assert "one project now: 3 note(s) and 1 task(s) moved to it, 1 repeated note(s) merged" in said
    # The repository outside the folder is linked in under its own name, and every repository answers to the folder.
    assert (folder / "web").is_symlink() and (folder / "web").resolve() == web.resolve()
    key = str(folder.resolve())
    assert repo.key(api) == repo.key(web) == repo.key(folder / "web") == repo.key(folder) == key
    assert repo.own_key(api) == before["api"]
    assert project.find(web) == folder / ".locks" and project.find(api) == folder / ".locks"
    # One memory: the notes are the folder's, the copy merged, each anchor named with its repository in front.
    live = store.notes(key, states=notes.LIVE)
    assert sorted(n["title"] for n in live) == ["Client", "Money"]
    assert [a["path"] for a in store.note_anchors([anchored])] == ["web/client.ts"]
    assert store.list_tasks(repo=key, source=("locks",))[0]["title"] == "old work"
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
    with pytest.raises(workspaces.WorkspaceError, match="is in a git checkout"):
        workspaces.add(lone / "api")
    with pytest.raises(workspaces.WorkspaceError, match="is in a git checkout"):
        workspaces.add(lone / "api" / "inner" / "folder")
    with pytest.raises(workspaces.WorkspaceError, match="is not in a git checkout"):
        workspaces.add(lone, [tmp_path])
    other = checkout(tmp_path / "other")
    workspaces.add(lone, [lone / "api", other, other])  # one given twice is one
    assert len(workspaces.declared()[0]["members"]) == 2
    second = tmp_path / "second"
    x = checkout(second / "x")
    with pytest.raises(workspaces.WorkspaceError, match="overlaps"):
        workspaces.add(second, [x, other])
    # A refused workspace left no link behind; a name already taken in the folder, or twice, is said.
    assert not (second / "other").exists()
    with pytest.raises(workspaces.WorkspaceError, match="named twice"):
        workspaces.add(second, [x, checkout(tmp_path / "a" / "same"), checkout(tmp_path / "b" / "same")])
    assert not (second / "same").exists()
    (second / "other").mkdir()
    with pytest.raises(workspaces.WorkspaceError, match="is already there"):
        workspaces.add(second, [x, checkout(tmp_path / "elsewhere" / "other")])
    # A repository deeper in the folder is a member, named by its path there.
    deep = tmp_path / "deep"
    entry = workspaces.add(deep, [checkout(deep / "services" / "api"), checkout(tmp_path / "front")])
    assert sorted(m["name"] for m in entry["members"]) == ["front", "services/api"]
    # Work still going in a repository holds the workspace back.
    busy = checkout(tmp_path / "busy")
    store.create_task("going", status="running", source="locks", repo=repo.key(busy), cwd=str(busy))
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
    from locks.orchestrate import Options, run

    from .test_orchestrate import kind

    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    web = checkout(folder / "web")
    dry = run("wire the client to the new endpoint", api, store, Options(dry_run=True, add_dirs=(str(web),)),
              registry={}, classifier=kind("implement"))
    offer = [r for r in dry.plan.reasons if r.startswith("this task spans")]
    assert len(offer) == 1 and f"locks workspace add {folder}" in offer[0] and "--with" in offer[0]
    workspaces.add(folder)
    dry = run("wire the client to the new endpoint", api, store, Options(dry_run=True, add_dirs=(str(web),)),
              registry={}, classifier=kind("implement"))
    assert not [r for r in dry.plan.reasons if r.startswith("this task spans")]


def test_a_workspace_keeps_the_team_and_the_rules_its_repositories_had(tmp_path, store: Store, capsys):
    from locks import grants, teams

    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    web = checkout(folder / "web")
    assert cli.main(["team", "create", "web", "--from", "locks", "--description", "web"]) == 0
    teams.use(repo.key(api), "web")
    grants.grant(repo.key(web), ["Bash(npm test:*)"])
    assert cli.main(["workspace", "add", str(folder)]) == 0
    key = str(folder.resolve())
    assert teams.assignments()[key] == "web" and "Bash(npm test:*)" in grants.granted(key)
    # Two repositories that used two teams: the person picks the folder's.
    other = tmp_path / "two"
    one = checkout(other / "one")
    two = checkout(other / "two")
    assert cli.main(["team", "create", "api", "--from", "locks", "--description", "api"]) == 0
    teams.use(repo.key(one), "web")
    teams.use(repo.key(two), "api")
    capsys.readouterr()
    assert cli.main(["workspace", "add", str(other)]) == 0
    assert "used the teams api, web" in capsys.readouterr().out and str(other.resolve()) not in teams.assignments()


def test_a_worktree_of_a_member_reads_its_notes_as_the_member_does(tmp_path, store: Store):
    folder = tmp_path / "shop"
    api = checkout(folder / "api", "cart.py")
    checkout(folder / "web")
    workspaces.add(folder)
    key = repo.key(api)
    note = store.add_note(key, "code", "Cart", "The cart rounds once.", author="person", filed_by="person")
    notes.anchor(store, note, api, ["cart.py"])
    tree = api / ".claude" / "worktrees" / "x"
    git(api, "worktree", "add", "-q", str(tree))
    assert workspaces.prefix(tree) == "api/" and repo.key(tree) == key
    # Nothing changed: nothing to review, from the worktree as from the checkout.
    assert notes.review(store, key, tree) == []
    notes.confirm(store, note, tree)
    (anchored,) = store.note_anchors([note])
    assert anchored["path"] == "api/cart.py" and anchored["blob"]


def test_the_folder_anchors_reviews_and_confirms_through_each_member(tmp_path, store: Store):
    folder = tmp_path / "shop"
    api = checkout(folder / "api", "cart.py")
    checkout(folder / "web")
    workspaces.add(folder)
    key = repo.key(folder)
    note = store.add_note(key, "code", "Cart", "The cart rounds once.", author="person", filed_by="person")
    # A run in the folder names files with the member in front.
    notes.anchor(store, note, folder, ["api/cart.py"])
    (anchored,) = store.note_anchors([note])
    assert anchored["path"] == "api/cart.py" and anchored["blob"]
    notes.confirm(store, note, folder)
    assert store.note_anchors([note])[0]["blob"] == anchored["blob"]
    (api / "cart.py").write_text("print('changed')\n")
    git(api, "commit", "-qam", "change")
    assert [n for n, _ in notes.review(store, key, folder)] == [note]


def test_moving_a_repository_in_keeps_its_paths_and_other_clones(tmp_path, store: Store, capsys):
    folder = tmp_path / "shop"
    lib = checkout(folder / "mylib", "setup.py")
    (lib / "mylib").mkdir()
    (lib / "mylib" / "core.py").write_text("x = 1\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "package")
    git(lib, "remote", "add", "origin", "https://example.com/o/mylib.git")
    other = checkout(tmp_path / "src" / "mylib")
    git(other, "remote", "add", "origin", "https://example.com/o/mylib.git")
    web = checkout(folder / "web")
    shared = repo.key(lib)
    assert repo.key(other) == shared
    note = store.add_note(shared, "code", "Core", "The core holds x.", author="person", filed_by="person")
    notes.anchor(store, note, lib, ["mylib/core.py"])
    mine = store.create_task("here", status="done", source="locks", repo=shared, cwd=str(lib))
    theirs = store.create_task("there", status="done", source="locks", repo=shared, cwd=str(other))
    store._conn.commit()
    assert cli.main(["workspace", "add", str(folder)]) == 0
    key = repo.key(web)
    # The task done in this clone moved; the other clone keeps its own, and its notes too (copied).
    assert store.get_task(mine["id"])["repo"] == key and store.get_task(theirs["id"])["repo"] == shared
    assert [n["title"] for n in store.notes(shared)] == ["Core"]
    (copied,) = store.notes(key)
    # A package named as its repository keeps its path, with the repository in front.
    assert [a["path"] for a in store.note_anchors([copied["id"]])] == ["mylib/mylib/core.py"]
    assert notes.review(store, key, lib) == []


def test_a_merged_note_keeps_the_anchors_and_links_of_its_copy(tmp_path, store: Store):
    folder = tmp_path / "shop"
    api = checkout(folder / "api", "money.ts")
    web = checkout(folder / "web", "money.ts")
    first = store.add_note(repo.key(api), "business", "Money", "Money is integer cents.", author="person",
                           filed_by="person")
    store.update_note(first, state="review", state_reason="changed")
    second = store.add_note(repo.key(web), "business", "Money", "Money is integer cents.", author="person",
                            filed_by="person")
    notes.anchor(store, second, web, ["money.ts"])
    prices = store.add_note(repo.key(web), "business", "Prices", "Prices are shown with tax.", author="person",
                            filed_by="person")
    notes.link(store, prices, second, "depends_on")
    (web / ".locks").mkdir()
    (web / ".locks" / "sessions.json").write_text(json.dumps({"s1": {"name": "my payments thread"}}))
    assert cli.main(["workspace", "add", str(folder)]) == 0
    key = repo.key(api)
    # The current copy stays, with the anchors and links of both; the session name the person wrote too.
    assert store.get_note(first)["state"] == "replaced" and store.get_note(second)["state"] == "current"
    assert [a["path"] for a in store.note_anchors([second])] == ["web/money.ts"]
    assert [n["id"] for n in notes.recall(store, key, "", paths=["web/money.ts"], limit=5, hops=0)] == [second]
    assert {e["from_id"] for e in store.note_links([second])} == {prices}
    assert project.names(folder / ".locks")["s1"]["name"] == "my payments thread"


def test_a_repository_keeps_its_own_workflows_in_a_workspace(tmp_path, store: Store):
    from locks import workflows

    folder = tmp_path / "shop"
    api = checkout(folder / "api")
    checkout(folder / "web")
    (api / ".locks" / "workflows").mkdir(parents=True)
    (api / ".locks" / "workflows" / "release.json").write_text(json.dumps(
        {"name": "release", "steps": [{"id": "do", "prompt": "Ship it", "kind": "docs"}]}))
    workspaces.add(folder)
    assert [w["name"] for w in workflows.listing(api) if w["scope"] == "project"] == ["release"]


def test_the_offer_never_takes_in_repositories_that_were_not_named(tmp_path, store: Store):
    from locks.orchestrate import _spans

    code = tmp_path / "code"
    api, web = checkout(code / "api"), checkout(code / "web")
    checkout(code / "blog")
    offer = _spans(api, [str(web)])
    # No folder holds just these two: no path is made up, the person makes one.
    assert "locks workspace add <folder>" in offer and "api-web" not in offer and str(code) not in offer
    assert "a workspace needs a folder that holds just them" in offer and "the person's choice" in offer
    (code / "blog").rename(tmp_path / "blog")
    offer = _spans(api, [str(web)])
    assert f"locks workspace add {code} --with {api} --with {web}" in offer and "the person's choice" in offer
    assert "moves what each repository kept" in offer


def test_a_malformed_member_is_left_out(tmp_path):
    home().mkdir(parents=True, exist_ok=True)
    (home() / workspaces.FILE).write_text(json.dumps({"workspaces": [
        {"folder": str(tmp_path), "members": ["not a member", {"name": "x"}, {"name": "a", "path": str(tmp_path)}]}]}))
    assert [m["name"] for m in workspaces.declared()[0]["members"]] == ["a"]
    assert workspaces.prefix(tmp_path) == "a/" and workspaces.of(tmp_path / "x") == tmp_path
