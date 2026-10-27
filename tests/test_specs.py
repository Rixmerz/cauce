"""A repository's OpenSpec specs against its code: how far behind, the brief, the ending."""
from __future__ import annotations

from cauce import hooks, specs
from cauce.orchestrate import run

from .conftest import git
from .test_orchestrate import Script, kind, ok


def _behind(repo, code_commits=6):
    (repo / "openspec" / "specs" / "login").mkdir(parents=True, exist_ok=True)
    (repo / "openspec" / "specs" / "login" / "spec.md").write_text("# login\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "specs")
    for n in range(code_commits):
        (repo / f"feature{n}.py").write_text(f"x = {n}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"feature {n}")


def test_drift_counts_the_code_commits_since_the_specs_last_changed(git_repo):
    assert specs.drift(git_repo) is None  # no openspec/: nothing to say
    _behind(git_repo, code_commits=2)
    found = specs.drift(git_repo)
    assert found.code_commits == 2 and not found.behind and found.days == 0
    (git_repo / "openspec" / "specs" / "login" / "spec.md").write_text("# login, again\n")
    git(git_repo, "commit", "-qam", "specs again")
    assert specs.drift(git_repo).code_commits == 0
    for n in range(specs.BEHIND_COMMITS):
        (git_repo / f"more{n}.py").write_text("y = 1\n")
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-q", "-m", "more")
    assert specs.drift(git_repo).behind and "code commit(s) ago" in specs.drift(git_repo).text()


def test_a_writing_worker_is_told_to_keep_the_specs_and_the_ending_offers_to_catch_them_up(git_repo, store):
    _behind(git_repo)
    script = Script(ok(), write="app.py")
    report = run("add a logout button", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), session_id="s1")
    assert report.status == "done"
    brief = script.specs[0].prompt
    assert "OpenSpec specs (`openspec/`)" in brief and "Its specs are behind the code" in brief
    planned = store.last_event(report.task_id, "planned")["data"]
    assert any(r.startswith("specs: ") for r in planned["reasons"])
    finished = store.final_event(report.task_id)["data"]
    assert finished["specs"]["code_commits"] >= specs.BEHIND_COMMITS
    # The session that sent it hears it with its ending, and the command that would catch them up.
    assert any("touches no spec" in line for line in report.impact)  # what `cauce run` prints
    text, _ = hooks.endings(store, "s1")
    assert "touches no spec" in text and f"--after task:{report.task_id}" in text and "Change no code." in text


def test_a_change_that_wrote_a_spec_or_a_reading_task_hears_nothing(git_repo, store):
    _behind(git_repo)
    found = specs.drift(git_repo).data()
    assert specs.ending_lines({"id": 1}, ["openspec/specs/login/spec.md", "app.py"], found, after="task:1") == []
    assert specs.ending_lines({"id": 1}, ["app.py"], None, after="task:1") == []
    script = Script(ok("read it"))
    run("where is the login handled", git_repo, store, registry={}, launcher=script, classifier=kind("explore"))
    assert "OpenSpec" not in script.specs[0].prompt


def test_with_the_setting_off_specs_are_only_read(git_repo, store, monkeypatch):
    _behind(git_repo)
    monkeypatch.setenv("CAUCE_SPECS", "off")
    script = Script(ok(), write="app.py")
    report = run("add a logout button", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "OpenSpec" not in script.specs[0].prompt and "specs" not in store.final_event(report.task_id)["data"]
