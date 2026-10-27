"""A repository's OpenSpec specs against its code: how far behind, the brief, the ending."""
from __future__ import annotations

from cauce import hooks, repo, specs
from cauce.cli import main
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


class _Builder:
    """A launcher whose worker writes code on every attempt and the spec from attempt `spec_on`."""

    def __init__(self, spec_on: int | None):
        self.spec_on, self.specs = spec_on, []

    def __call__(self, spec):
        self.specs.append(spec)
        (spec.target_dir / "app.py").write_text(f"attempt {len(self.specs)}\n")
        if self.spec_on is not None and len(self.specs) >= self.spec_on:
            (spec.target_dir / "openspec" / "specs" / "login" / "spec.md").write_text("# login, with logout\n")
        return ok()


def test_a_spec_first_build_that_wrote_no_spec_goes_back_to_its_worker_once(git_repo, store):
    _behind(git_repo, code_commits=1)
    specs.set_first(store, repo.key(git_repo), True)
    builder = _Builder(spec_on=2)
    report = run("add a logout button", git_repo, store, registry={}, launcher=builder, classifier=kind("implement"))
    assert report.status == "done" and len(builder.specs) == 2
    assert "spec-first" in builder.specs[0].prompt  # the brief says it before any code
    owed = store.last_event(report.task_id, "spec_owed")["data"]
    assert owed["paths"] == ["app.py"] and not owed["again"]
    assert any(r.startswith("spec-first: ") for r in store.last_event(report.task_id, "planned")["data"]["reasons"])


def test_a_second_build_without_its_spec_is_kept_for_the_orchestrator_to_decide(git_repo, store):
    _behind(git_repo, code_commits=1)
    specs.set_first(store, repo.key(git_repo), True)
    builder = _Builder(spec_on=None)
    report = run("add a logout button", git_repo, store, registry={}, launcher=builder, classifier=kind("implement"))
    assert len(builder.specs) == 2 and report.status != "done" and report.stop.cause == "decision"
    assert "spec-first" in report.stop.reason and report.branch  # the work is kept on its branch


def test_spec_first_binds_only_builds_and_only_where_the_person_turned_it_on(git_repo, store):
    _behind(git_repo, code_commits=1)
    report = run("add a logout button", git_repo, store, registry={}, launcher=_Builder(None),
                 classifier=kind("implement"))
    assert report.status == "done"  # off by default
    specs.set_first(store, repo.key(git_repo), True)
    report = run("tidy the logout button", git_repo, store, registry={}, launcher=_Builder(None),
                 classifier=kind("refactor"))
    assert report.status == "done"  # a refactor changes no behaviour
    assert specs.owed("feature", ["openspec/changes/x/proposal.md", "app.py"]) == []
    assert specs.owed("feature", ["README.md", "docs/a.md"]) == []
    assert specs.owed("feature", ["README.md", "src/a.py"]) == ["src/a.py"]


def test_cauce_specs_says_how_far_behind_and_turns_spec_first_on(git_repo, capsys):
    _behind(git_repo)
    assert main(["specs", "--repo", str(git_repo)]) == 0
    assert "behind" in capsys.readouterr().out
    assert main(["specs", "--repo", str(git_repo), "first", "on"]) == 0
    assert "spec-first: on" in capsys.readouterr().out
    main(["specs", "--repo", str(git_repo), "first", "off"])
    assert "spec-first: off" in capsys.readouterr().out


def test_the_worker_asked_again_is_told_to_write_the_spec_not_to_reformat(git_repo):
    from cauce import launch
    from cauce.escalate import Attempt, Cell
    from cauce.orchestrate import _reshape_prompt, continuation_note

    held = Attempt(Cell("sonnet", "medium"), 30, False, None, "built it", shape=specs.first_shape(["app.py"]))
    unread = Attempt(Cell("sonnet", "medium"), 30, False, None, "built it", shape="two result blocks")
    assert _reshape_prompt([held]) == specs.FIRST_PROMPT and _reshape_prompt([unread]) == launch.RESHAPE_PROMPT
    assert continuation_note(held).startswith("Why your last answer was held back: spec-first")
