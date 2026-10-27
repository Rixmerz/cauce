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


# --- spec-code links -------------------------------------------------------------

LOGIN_SPEC = """# login

## Purpose
Who may sign in.

## Requirements
### Requirement: Valid credentials
The system SHALL sign a user in with valid credentials.

#### Scenario: ok
- **WHEN** the password is right
- **THEN** the user is signed in

### Requirement: Lockout after failures
The system SHALL lock an account after five failures.
"""


def _spec_repo(repo):
    (repo / "openspec" / "specs" / "login").mkdir(parents=True)
    (repo / "openspec" / "specs" / "login" / "spec.md").write_text(LOGIN_SPEC)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "specs")
    return git(repo, "rev-parse", "HEAD")


def test_a_spec_id_is_the_capability_and_the_requirement_name_as_livespec_derives_it():
    assert specs.slug("Valid credentials!") == "valid-credentials"
    assert specs.slug("  Retry (3x) -- then stop ") == "retry-3x-then-stop"
    assert specs.slug("???") == "requirement"
    found = specs._requirements("openspec/specs/Login Flow/spec.md", LOGIN_SPEC)
    assert [(r.spec_id, r.added) for r in found] == [("login-flow-valid-credentials", True),
                                                     ("login-flow-lockout-after-failures", True)]


ACCENTED_SPEC = """# acceso

## Requirements
### Requirement: Validación de contraseñas
El sistema DEBE validar la contraseña.

### Requirement: Valid credentials
<!-- livespec:id=SPEC-007 -->
The system SHALL sign a user in with valid credentials.
"""


def test_a_spec_id_folds_accents_as_livespec_does_and_keeps_the_old_one_as_its_alias():
    # livespec transliterates (`validación` -> `validacion`); before it did, a non-ASCII letter became a hyphen.
    assert specs.slug("Validación de contraseñas") == "validacion-de-contrasenas"
    assert specs.slug("Straße Ørsted") == "strasse-orsted"
    found = specs._requirements("openspec/specs/acceso/spec.md", ACCENTED_SPEC)
    assert found[0].spec_id == "acceso-validacion-de-contrasenas"
    assert found[0].legacy == "acceso-validaci-n-de-contrase-as"
    assert found[1].legacy == "", "an id the heading derives the same either way has no alias"


def test_a_livespec_id_marker_under_the_heading_is_the_requirements_id():
    found = specs._requirements("openspec/specs/acceso/spec.md", ACCENTED_SPEC)
    assert found[1].spec_id == "SPEC-007"
    # A marker is read only before the prose: one after it is the prose's own text.
    late = "### Requirement: Late\nThe system SHALL do it.\n<!-- livespec:id=SPEC-008 -->\n"
    assert specs._requirements("openspec/specs/acceso/spec.md", late)[0].spec_id == "acceso-late"


def test_a_comment_with_the_accent_folded_id_the_marker_id_or_the_old_id_links_the_spec(git_repo):
    (git_repo / "openspec" / "specs" / "acceso").mkdir(parents=True)
    spec = git_repo / "openspec" / "specs" / "acceso" / "spec.md"
    spec.write_text("# acceso\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "specs")
    base = git(git_repo, "rev-parse", "HEAD")
    spec.write_text(ACCENTED_SPEC)
    changed = ["openspec/specs/acceso/spec.md"]
    assert [r.spec_id for r in specs.unlinked(git_repo, base, changed)] == ["acceso-validacion-de-contrasenas",
                                                                            "SPEC-007"]
    (git_repo / "auth.py").write_text("# @spec:acceso-validacion-de-contrasenas, spec-007\ndef check(): ...\n")
    assert specs.unlinked(git_repo, base, changed) == []
    # Code annotated before livespec folded accents still links: livespec keeps the old id as an alias.
    (git_repo / "auth.py").write_text("# @spec:acceso-validaci-n-de-contrase-as\n# @spec:SPEC-007\ndef check(): ...\n")
    assert specs.unlinked(git_repo, base, changed) == []


def test_only_the_requirements_a_change_touched_are_its_own_and_new_ones_are_told_from_old(git_repo):
    base = _spec_repo(git_repo)
    spec = git_repo / "openspec" / "specs" / "login" / "spec.md"
    spec.write_text(LOGIN_SPEC.replace("with valid credentials.", "with valid credentials and a session.")
                    + "\n### Requirement: Password reset\nThe system SHALL mail a reset link.\n")
    found = specs.requirements_touched(git_repo, base, ["openspec/specs/login/spec.md"])
    assert [(r.spec_id, r.new) for r in found] == [("login-valid-credentials", False), ("login-password-reset", True)]
    # Nothing under openspec/specs/ changed: no requirement is the change's.
    assert specs.requirements_touched(git_repo, base, ["app.py"]) == []


def test_a_delta_adds_modifies_and_removes_and_only_what_it_adds_is_new(git_repo):
    base = _spec_repo(git_repo)
    delta = git_repo / "openspec" / "changes" / "add-2fa" / "specs" / "login"
    delta.mkdir(parents=True)
    (delta / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Two factor\nThe system SHALL ask for a code.\n\n"
        "## MODIFIED Requirements\n### Requirement: Valid credentials\nThe system SHALL also check a code.\n\n"
        "## REMOVED Requirements\n### Requirement: Lockout after failures\n")
    found = specs.requirements_touched(git_repo, base, ["openspec/changes/add-2fa/specs/login/spec.md"])
    assert [(r.spec_id, r.new) for r in found] == [("login-two-factor", True), ("login-valid-credentials", False)]


def test_a_spec_with_a_spec_comment_on_code_is_linked_and_one_without_is_not(git_repo):
    base = _spec_repo(git_repo)
    spec = git_repo / "openspec" / "specs" / "login" / "spec.md"
    spec.write_text(LOGIN_SPEC.replace("a user in", "a person in")
                    + "\n### Requirement: Password reset\nThe system SHALL mail a reset link.\n")
    changed = ["openspec/specs/login/spec.md"]
    assert [r.spec_id for r in specs.unlinked(git_repo, base, changed)] == ["login-valid-credentials",
                                                                            "login-password-reset"]
    # A comment in a new, untracked file counts; so does one of several ids, in any case.
    (git_repo / "auth.py").write_text("# @spec: Login-Password-Reset, login-valid-credentials\ndef reset(): ...\n")
    assert specs.annotated(git_repo) >= {"login-password-reset", "login-valid-credentials"}
    assert specs.unlinked(git_repo, base, changed) == []
    # Neither the spec itself nor a document is code: a mention there links nothing.
    (git_repo / "auth.py").unlink()
    (git_repo / "README.md").write_text("see @spec:login-password-reset\n")
    spec.write_text(spec.read_text() + "\n<!-- @spec:login-valid-credentials -->\n")
    assert [r.spec_id for r in specs.unlinked(git_repo, base, changed)] == ["login-valid-credentials",
                                                                            "login-password-reset"]


def test_a_change_that_touches_no_spec_owes_no_links(git_repo):
    base = _spec_repo(git_repo)
    (git_repo / "app.py").write_text("print('changed')\n")
    assert specs.unlinked(git_repo, base, ["app.py"]) == []


def test_a_spec_written_without_links_blocks_and_an_old_one_is_a_warning():
    new = specs.Requirement("login-password-reset", "openspec/specs/login/spec.md", True)
    old = specs.Requirement("login-valid-credentials", "openspec/specs/login/spec.md", False)
    first, second = specs.link_lines([new, old])
    assert first.startswith(specs.LINK_PREFIX) and "wrote `login-password-reset`" in first
    assert "`@spec:login-password-reset`" in first and "blocks" in first and "warning" not in first
    assert "changed `login-valid-credentials`" in second and "a warning, not a blocker" in second
    assert specs.link_lines([]) == []


class _Writes:
    """A launcher whose worker writes `files` (path -> text) and whose verifier passes, recording both."""

    def __init__(self, files):
        from .test_checking import check, passing

        self.files, self.seen, self._check, self._passing = files, [], check("pass"), passing()

    def __call__(self, spec):
        self.seen.append(spec)
        if "Check this change" in spec.prompt:
            return self._check
        for path, text in self.files.items():
            (spec.target_dir / path).parent.mkdir(parents=True, exist_ok=True)
            (spec.target_dir / path).write_text(text)
        return self._passing

    @property
    def verifier_prompts(self):
        return [s.prompt for s in self.seen if "Check this change" in s.prompt]


NEW_REQUIREMENT = LOGIN_SPEC + "\n### Requirement: Password reset\nThe system SHALL mail a reset link.\n"


def _link_run(git_repo, store, tmp_path, monkeypatch, files, *, indexed=True, of_kind="implement"):
    from .livespec_fixture import build
    from .test_orchestrate import _livespec

    monkeypatch.setenv("CAUCE_VERIFIER", "on")
    if not (git_repo / "openspec").exists():
        _spec_repo(git_repo)
    if indexed:
        build(git_repo)
    else:
        (git_repo / ".mcp-docs" / "docs.db").unlink(missing_ok=True)
    writes = _Writes(files)
    report = run("fix the cart total", git_repo, store, registry={}, launcher=writes, classifier=kind(of_kind),
                 adapters=[_livespec(tmp_path)] if indexed else [])
    return report, writes


def _links_said(report):
    return [line for line in report.impact if line.startswith(specs.LINK_PREFIX)]


def test_a_spec_the_worker_wrote_with_no_comment_is_said_and_told_to_the_independent_check(
        git_repo, store, tmp_path, monkeypatch):
    # Code with no test beside it is high risk, so the independent check runs.
    report, writes = _link_run(git_repo, store, tmp_path, monkeypatch,
                               {"openspec/specs/login/spec.md": NEW_REQUIREMENT, "reset.py": "def mail(): ...\n"})
    assert report.status == "done"
    said = _links_said(report)
    assert len(said) == 1 and "wrote `login-password-reset`" in said[0] and "blocks" in said[0]
    prompt, = writes.verifier_prompts
    assert "`@spec:<id>` comment" in prompt and "cauce found these without the comment:" in prompt
    assert "- the change wrote `login-password-reset`" in prompt and specs.LINK_PREFIX not in prompt
    assert "a warning, never a blocker" in prompt


def test_a_spec_whose_functions_carry_the_comment_is_no_finding(git_repo, store, tmp_path, monkeypatch):
    report, writes = _link_run(git_repo, store, tmp_path, monkeypatch, {
        "openspec/specs/login/spec.md": NEW_REQUIREMENT,
        "auth.py": "# @spec:login-password-reset\ndef mail_reset_link(): ...\n"})
    assert report.status == "done"
    assert _links_said(report) == []
    assert writes.verifier_prompts
    assert all("cauce found these without the comment" not in p for p in writes.verifier_prompts)
    assert not any(NO_LINK_RISK in reason for reason in report.plan.risk.reasons)


NO_LINK_RISK = "it wrote a spec that no code names in an `@spec:` comment"


def test_a_spec_written_with_no_comment_reaches_the_independent_check_whatever_the_risk_was(
        git_repo, store, tmp_path, monkeypatch):
    # A docs task starts passive and writes no code, so no tier of its own would run the check that blocks.
    report, writes = _link_run(git_repo, store, tmp_path, monkeypatch,
                               {"openspec/specs/login/spec.md": NEW_REQUIREMENT}, of_kind="docs")
    assert report.status == "done"
    assert report.plan.risk.tier == "high"
    assert NO_LINK_RISK in report.plan.risk.reasons
    prompt, = writes.verifier_prompts
    assert "cauce found these without the comment:" in prompt


def test_a_spec_that_was_already_there_raises_no_risk_and_runs_no_check(git_repo, store, tmp_path, monkeypatch):
    edited = LOGIN_SPEC.replace("a user in", "a person in")
    report, writes = _link_run(git_repo, store, tmp_path, monkeypatch,
                               {"openspec/specs/login/spec.md": edited}, of_kind="docs")
    assert report.status == "done"
    assert len(_links_said(report)) == 1
    assert report.plan.risk.tier == "passive"
    assert NO_LINK_RISK not in report.plan.risk.reasons
    assert writes.verifier_prompts == []


def test_a_spec_that_was_already_there_is_a_warning_and_a_repository_with_no_index_is_not_asked(
        git_repo, store, tmp_path, monkeypatch):
    edited = LOGIN_SPEC.replace("a user in", "a person in")
    report, _ = _link_run(git_repo, store, tmp_path, monkeypatch,
                          {"openspec/specs/login/spec.md": edited, "login.py": "def sign_in(): ...\n"})
    said = _links_said(report)
    assert len(said) == 1 and "changed `login-valid-credentials`" in said[0] and "a warning, not a blocker" in said[0]
    # Workers there were never told to link (no livespec index): nothing is checked against what they were not asked.
    bare, writes = _link_run(git_repo, store, tmp_path, monkeypatch,
                             {"openspec/specs/login/spec.md": NEW_REQUIREMENT, "reset.py": "def mail(): ...\n"},
                             indexed=False)
    assert _links_said(bare) == []
    assert writes.verifier_prompts
    assert all("cauce found these without the comment" not in p for p in writes.verifier_prompts)


def test_links_are_expected_only_where_specs_and_a_livespec_index_are_both_there(
        git_repo, tmp_path, monkeypatch):
    from .livespec_fixture import build

    _spec_repo(git_repo)
    assert not specs.links_expected(git_repo, {})  # no livespec index yet
    build(git_repo)
    assert specs.links_expected(git_repo, {})
    assert not specs.links_expected(git_repo, {"CAUCE_SPECS": "off"})
    assert not specs.links_expected(git_repo, {"CAUCE_LIVESPEC": "off"})
    assert not specs.links_expected(tmp_path / "nowhere", {})
    assert not specs.links_expected(None, {})
