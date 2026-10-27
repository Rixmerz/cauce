"""A repository's OpenSpec specs against its code: how far behind, the brief, the ending."""
from __future__ import annotations

import pytest

from cauce import hooks, repo, specs
from cauce.adapters.livespec import LivespecError
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


def test_spec_first_on_in_a_repository_with_no_openspec_says_nothing_is_enforced(git_repo, capsys):
    assert main(["specs", "--repo", str(git_repo), "first", "on"]) == 0
    out = capsys.readouterr().out
    assert "spec-first is on" in out and "nothing is enforced" in out and "no `openspec/` here" in out
    # Said on every look while it stands, not only the one that turned it on.
    assert main(["specs", "--repo", str(git_repo)]) == 0
    assert "nothing is enforced" in capsys.readouterr().out
    # Off, or with the folder there, it says something else.
    main(["specs", "--repo", str(git_repo), "first", "off"])
    assert "nothing is enforced" not in capsys.readouterr().out
    _behind(git_repo)
    main(["specs", "--repo", str(git_repo), "first", "on"])
    assert "nothing is enforced" not in capsys.readouterr().out


@pytest.mark.parametrize("path", [
    "src/app.py", "lib/pay.ts", "main.go", "web/Cart.tsx", "src/lib.rs", "scripts/deploy.sh",
    "src/build/compiler.py", "lib/out/render.ts", "internal/target/plan.go",  # below the root they are names
])
def test_files_that_hold_behaviour_owe_a_spec(path):
    assert specs.holds_behaviour(path)


@pytest.mark.parametrize("path", [
    "tests/test_cart.py", "src/cart.test.ts", "src/__tests__/a.js", "conftest.py", "tests/conftest.py",  # tests
    "package-lock.json", "web/yarn.lock", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum", "pnpm-lock.yaml",  # locks
    "pyproject.toml", "package.json", "tsconfig.json", ".eslintrc", ".github/workflows/ci.yml", "config/app.yaml",
    "vite.config.ts", "jest.config.js", "Dockerfile", "Makefile", ".gitignore", "settings.ini",
    ".env.example",  # config
    "dist/app.js", "build/out.css", "web/app.min.js", "src/api_pb2.py", "src/__snapshots__/a.snap",
    "node_modules/x/index.js", "src/types.generated.ts", "out/main.js", "target/classes/App.js",  # generated
    "README.md", "docs/guide.html", "openspec/specs/a/spec.md", "notes.txt",  # documents and specs
])
def test_files_that_hold_no_behaviour_owe_no_spec(path):
    assert not specs.holds_behaviour(path)


def test_a_build_that_changed_only_tests_config_and_locks_owes_no_spec_but_one_with_code_does():
    only = ["tests/test_a.py", "package-lock.json", "pyproject.toml", "dist/bundle.js"]
    assert specs.owed("feature", only) == []
    assert specs.owed("feature", [*only, "src/a.py"]) == ["src/a.py"]


def test_structure_lines_name_each_kind_of_problem_and_say_when_there_is_none():
    clean = {"valid": True, "error_count": 0, "warning_count": 0, "findings": []}
    assert specs.structure_lines(clean) == ["structure: livespec's validate_openspec found no structural problem"]
    found = {"error_count": 1, "warning_count": 2, "findings": [
        {"issue": "requirement has no scenario", "severity": "error", "count": 4,
         "sample": [{"spec_id": "cart-1"}, {"spec_id": "cart-2"}]},
        {"issue": "no normative keyword", "severity": "warning", "count": 2, "sample": []}]}
    assert specs.structure_lines(found) == [
        "structure: livespec's validate_openspec found 1 error(s) and 2 warning(s)",
        "  - error: requirement has no scenario (4) e.g. cart-1, cart-2",
        "  - warning: no normative keyword (2)"]
    many = {"error_count": 0, "warning_count": 9, "findings": [{"issue": f"kind {n}", "count": 1} for n in range(10)]}
    assert specs.structure_lines(many)[-1] == "  - and 2 more kind(s) of finding"


class _Asked:
    """A livespec that answers `validate_openspec` as scripted, or cannot be asked."""

    def __init__(self, answer=None, error=None, server=True, indexed=True, within=("done", "indexed")):
        self.answer, self.error, self._server = answer, error, server
        self.indexed, self.within, self.waited = indexed, within, []

    def server(self):
        return {"command": "livespec"} if self._server else None

    def inspect(self, repo_dir):
        from cauce.adapters import Status

        return Status("livespec", "present" if self.indexed else "absent")

    def refresh_within(self, repo_dir, wait_s):
        self.waited.append(wait_s)
        return self.within

    def validate_openspec(self, repo_dir):
        if self.error:
            raise LivespecError(self.error)
        return self.answer


def test_structure_asks_livespec_where_it_runs_and_says_when_it_could_not(git_repo, monkeypatch):
    monkeypatch.setenv("CAUCE_LIVESPEC", "on")
    answer = {"error_count": 0, "warning_count": 1, "findings": [{"issue": "no scenario", "count": 1}]}
    assert specs.structure(git_repo, _Asked(answer)) == [
        "structure: livespec's validate_openspec found 0 error(s) and 1 warning(s)", "  - warning: no scenario (1)"]
    assert specs.structure(git_repo, _Asked(error="workspace not indexed")) == [
        "structure: livespec could not validate the specs (workspace not indexed)"]
    assert specs.structure(git_repo, _Asked(server=False)) == []  # nothing to ask: nothing said
    monkeypatch.setenv("CAUCE_LIVESPEC", "off")
    assert specs.structure(git_repo, _Asked(answer)) == []


def test_cauce_specs_reports_the_structure_after_the_drift(git_repo, capsys, monkeypatch):
    _behind(git_repo)
    monkeypatch.setattr(specs, "structure", lambda where, adapter=None: ["structure: livespec says so"])
    assert main(["specs", "--repo", str(git_repo)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("specs: ") and out[-1] == "structure: livespec says so"


def test_structure_indexes_a_repository_with_no_index_first_and_says_when_it_could_not(git_repo, monkeypatch):
    monkeypatch.setenv("CAUCE_LIVESPEC", "on")
    monkeypatch.setenv("CAUCE_INDEX_WAIT", "20")
    answer = {"error_count": 0, "warning_count": 0, "findings": []}
    built = _Asked(answer, indexed=False)
    assert specs.structure(git_repo, built) == ["structure: livespec's validate_openspec found no structural problem"]
    assert built.waited == [20]
    large = _Asked(answer, indexed=False, within=("large", "5000 tracked files take longer than the 20 s"))
    assert specs.structure(git_repo, large) == [
        "structure: not validated, livespec has no index here yet (5000 tracked files take longer than the 20 s); "
        "the next task in this repository indexes it"]
    monkeypatch.setenv("CAUCE_INDEX_WAIT", "0")
    never = _Asked(answer, indexed=False)
    assert "index_wait is 0" in specs.structure(git_repo, never)[0] and never.waited == []
