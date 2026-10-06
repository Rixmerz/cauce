from __future__ import annotations

import json
import os

from cauce import hooks, naming, project
from cauce.store import Store
from cauce.ui import api

from .test_classify import _reply, _runner


def test_a_project_is_enrolled_by_its_cauce_folder(git_repo, tmp_path):
    sub = git_repo / "pkg"
    sub.mkdir()
    assert project.find(sub) is None and project.find(None) is None
    folder = project.enroll(sub)
    assert folder == git_repo / ".cauce"  # at the top of the checkout, not where the session runs
    assert (folder / ".gitignore").read_text().endswith("*\n")
    assert project.find(sub) == folder and project.enroll(git_repo) == folder
    # The search stops at the checkout: a .cauce/ above it is another project's.
    (tmp_path / ".cauce").mkdir()
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    assert project.find(other) is None
    # Outside any checkout, the session's own directory is the project.
    loose = tmp_path / "loose" / "dir"
    loose.mkdir(parents=True)
    assert project.enroll(loose) == loose / ".cauce"


def test_a_cauce_folder_above_a_project_enrolls_no_session_below_it(store: Store, tmp_path, monkeypatch):
    """A `.cauce/` in a folder that holds several projects, or in the home
    directory, put every session on the machine in the UI."""
    home = tmp_path / "home"
    monkeypatch.setattr(project.Path, "home", classmethod(lambda cls: home))
    container = home / "projects"
    loose, repo = container / "notes", container / "app"
    for d in (loose / "deep", repo / "src"):
        d.mkdir(parents=True)
    (repo / ".git").mkdir()
    (container / ".cauce").mkdir()  # cauce once ran in the container itself
    (home / ".cauce").mkdir()
    assert project.find(container) == container / ".cauce"  # that session is its own
    assert project.find(loose) is None and project.find(loose / "deep") is None
    assert project.find(repo / "src") is None
    assert project.find(home) is None and project.find(home / "Downloads") is None
    # a home that is a git checkout (dotfiles) is no project either
    (home / ".git").mkdir()
    assert project.find(loose / "deep") is None
    assert project.enroll(loose / "deep") == loose / "deep" / ".cauce"
    for never in (home, project.Path("/")):
        try:
            project.enroll(never)
        except project.NotAProject as exc:
            assert "is not a project" in str(exc)
        else:
            raise AssertionError(f"{never} was enrolled")
    for i, cwd in enumerate((container, loose, repo / "src", home, home / "Downloads")):
        store.touch_session(f"s{i}", str(cwd), "r")
    assert [s["id"] for s in api.sessions(store)] == ["s0"]


def test_a_name_haiku_gave_is_haiku_s_until_a_person_changes_it(tmp_path):
    folder = tmp_path / ".cauce"
    folder.mkdir()
    assert project.wants_name(None, 1) and not project.wants_name(None, 0)
    assert project.set_haiku_name(folder, "s", "Parallel dispatcher", 3, "t1")
    entry = project.names(folder)["s"]
    assert not project.wants_name(entry, 5) and project.wants_name(entry, 3 + project.RENAME_EVERY)
    assert project.name_of(tmp_path, "s") == {"name": "Parallel dispatcher", "by": "haiku"}
    # A person edits the file by hand: from then on it is theirs.
    data = json.loads((folder / "sessions.json").read_text())
    data["s"]["name"] = "My dispatcher work"
    (folder / "sessions.json").write_text(json.dumps(data))
    assert project.name_of(tmp_path, "s") == {"name": "My dispatcher work", "by": "you"}
    assert not project.wants_name(project.names(folder)["s"], 100)
    assert project.set_haiku_name(folder, "s", "Something else", 100, "t2") is False
    assert project.names(folder)["s"]["name"] == "My dispatcher work"
    (folder / "sessions.json").write_text("{broken")
    assert project.names(folder) == {} and project.name_of(tmp_path, "s") is None


def test_haiku_names_a_session_from_its_first_and_latest_prompts(store: Store, git_repo):
    project.enroll(git_repo)
    store.touch_session("s", str(git_repo), "r")
    for i in range(15):
        store.create_task(f"prompt {i}", status="done", source="hook", session_id="s", repo="r")
    run = _runner(_reply(name='"Cart total fix."'))
    assert naming.name_session(store, "s", runner=run) == "Cart total fix"
    asked = run.calls[0][1]["input"]
    assert "- prompt 0" in asked and "- prompt 14" in asked and "- prompt 7" not in asked
    assert project.names(git_repo / ".cauce")["s"]["prompts"] == 15
    # Named, and not enough new prompts since: no call.
    assert naming.name_session(store, "s", runner=_runner(raises=AssertionError("no call"))) is None
    # A session outside an enrolled project, or no answer, writes nothing.
    store.touch_session("t", "/nowhere", None)
    assert naming.name_session(store, "t", runner=run) is None
    store.touch_session("u", str(git_repo), "r")
    store.create_task("one", status="done", source="hook", session_id="u", repo="r")
    assert naming.name_session(store, "u", runner=_runner("not json")) is None
    assert naming.question(["a"]) == "First prompts:\n- a"


def test_stop_starts_a_naming_only_when_one_is_wanted(store: Store, git_repo, tmp_path, monkeypatch):
    started = []
    popen = lambda argv, **kw: started.append(argv)  # noqa: E731
    store.touch_session("s", str(git_repo), "r")
    store.create_task("fix the cart", status="done", source="hook", session_id="s", repo="r")
    root = tmp_path / "home"
    (root / "work").mkdir(parents=True)
    on = {**os.environ, "CAUCE_NAMES": "on"}
    assert naming.maybe_start(store, "s", root, on, popen=popen) is False  # not enrolled
    project.enroll(git_repo)
    assert naming.maybe_start(store, "s", root, {**on, "CAUCE_NAMES": "off"}, popen=popen) is False
    assert naming.maybe_start(store, "s", root, on, popen=popen) is True
    assert started[0][-2:] == ["name-session", "s"]
    project.set_haiku_name(git_repo / ".cauce", "s", "Cart", 1, "t")
    assert naming.maybe_start(store, "s", root, on, popen=popen) is False  # named, nothing new
    assert naming.maybe_start(store, "nobody", root, on, popen=popen) is False

    def broken(*a, **kw):
        raise OSError("no fork")

    store.touch_session("v", str(git_repo), "r")
    store.create_task("x", status="done", source="hook", session_id="v", repo="r")
    assert naming.maybe_start(store, "v", root, on, popen=broken) is False
    # The hook path itself: Stop never raises and asks for the naming.
    calls = []
    monkeypatch.setattr(naming, "maybe_start", lambda *a, **kw: calls.append(a[1]))
    hooks.stop({"session_id": "v"}, store)
    assert calls == ["v"]


def test_using_cauce_in_a_project_enrolls_it(store: Store, git_repo):
    event = {"session_id": "s", "prompt_id": "1", "cwd": str(git_repo), "prompt": "++ write the changelog"}
    hooks.user_prompt_submit(event, store)
    assert (git_repo / ".cauce").is_dir()
    assert store.messages(store.list_tasks()[0]["id"])[0]["role"] == "person"  # typed by the person


def test_the_ui_lists_only_enrolled_sessions_with_their_names(store: Store, git_repo, tmp_path):
    store.touch_session("in", str(git_repo), "r")
    store.touch_session("out", str(tmp_path), "r")
    assert api.sessions(store, {"r"}) == []
    project.enroll(git_repo)
    project.set_haiku_name(git_repo / ".cauce", "in", "Cart total fix", 1, "t")
    listed = api.sessions(store, {"r"})
    assert [(s["id"], s["name"], s["named_by"]) for s in listed] == [("in", "Cart total fix", "haiku")]
    assert api.session_detail(store, "out") is None
