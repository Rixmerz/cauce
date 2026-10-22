"""A red check compared with the same check at the base: what failed there too was there
before the work, never its failure. And a check whose exit says nothing is said."""
from __future__ import annotations

import json
import time

import pytest

from cauce import ci, doctor, hooks, isolate, launch, orchestrate
from cauce.launch import WorkerResult, check_failures
from cauce.orchestrate import Options, run
from cauce.store import Store

from .conftest import git
from .test_orchestrate import Script, kind, ok

ESC = "\x1b"

# --- which lines of a check's output are failures ------------------------------------------

JEST_COVERAGE = f"""\
> app@1.0.0 test
> jest --coverage

{ESC}[1m{ESC}[31m FAIL {ESC}[39m{ESC}[22m src/app/features/admin/users.spec.ts (5.123 s)
  UsersComponent
    ✓ renders (12 ms)
    ✕ lists the users (31 ms)

  ● UsersComponent › lists the users

    expect(received).toEqual(expected)

  ● Console

    console.log
      loaded

 PASS  src/app/core/auth.spec.ts
-----------|---------|----------|---------|---------|
File       | % Stmts | % Branch | % Funcs | % Lines |
-----------|---------|----------|---------|---------|
Jest: "src/app/features/admin/users.ts" coverage threshold for lines (80%) not met: 61.53%
Jest: "global" coverage threshold for branches (70%) not met: 64.2%
Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 3 passed, 4 total
Time:        7.24 s
npm error code 1
npm error path /work/app
"""

VITEST = """\
 ❯ src/cart.test.ts (3 tests | 1 failed) 8ms
   × cart > adds two items 4ms
     → expected 3 to be 2

 FAIL  src/cart.test.ts > cart > adds two items
AssertionError: expected 3 to be 2

 Test Files  1 failed (1)
      Tests  1 failed | 2 passed (3)
"""

PYTEST = """\
============================= test session starts ==============================
tests/test_tax.py .F.                                                    [ 50%]
tests/test_api.py E                                                      [100%]
=========================== short test summary info ============================
FAILED tests/test_tax.py::test_rounds[half-up] - assert 1.0 == 2.0
ERROR tests/test_api.py::test_login - ConnectionRefusedError: [Errno 111]
========================= 1 failed, 2 passed, 1 error in 0.42s =========================
"""

GO_TEST = """\
=== RUN   TestParse
--- FAIL: TestParse (0.00s)
    parse_test.go:12: got 1, want 2
FAIL
FAIL\texample.com/app/parse\t0.012s
ok  \texample.com/app/util\t(cached)
"""

TSC = """\
src/app/user.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/app/cart.ts:3:10 - error TS2304: Cannot find name 'Cart'.

Found 2 errors in 2 files.
"""

ESLINT_STYLISH = """\

/work/app/src/index.js
  1:7   error    'unused' is assigned a value but never used  no-unused-vars
  3:1   warning  Unexpected console statement                 no-console

/work/app/src/util.js
  10:3  error  Missing semicolon  semi

✖ 3 problems (2 errors, 1 warning)
"""

ISTANBUL = "ERROR: Coverage for lines (61.5%) does not meet global threshold (80%)\n"
MOCHA = "  2 passing (12ms)\n  1 failing\n\n  1) Cart\n       adds two:\n     AssertionError: expected 3\n"
DENO = """\
adds two ... FAILED (3ms)

 ERRORS

adds two => ./cart_test.ts:3:6
error: AssertionError: Values are not equal.

FAILED | 1 passed | 1 failed (12ms)

error: Test failed
error[no-unused-vars]: `x` is never used
 --> /work/app/main.ts:1:7
"""


@pytest.mark.parametrize(("output", "expected"), [
    (JEST_COVERAGE, {
        "FAIL src/app/features/admin/users.spec.ts",
        "✕ lists the users",
        "● UsersComponent › lists the users",
        'Jest: "src/app/features/admin/users.ts" coverage threshold for lines (80%) not met: 61.53%',
        'Jest: "global" coverage threshold for branches (70%) not met: 64.2%',
    }),
    (VITEST, {"✕ cart > adds two items", "FAIL src/cart.test.ts > cart > adds two items"}),
    (PYTEST, {"FAILED tests/test_tax.py::test_rounds[half-up]", "ERROR tests/test_api.py::test_login"}),
    (GO_TEST, {"--- FAIL: TestParse", "FAIL example.com/app/parse"}),
    (TSC, {"src/app/user.ts:12:5 error TS2322: Type 'string' is not assignable to type 'number'.",
           "src/app/cart.ts:3:10 error TS2304: Cannot find name 'Cart'."}),
    (ESLINT_STYLISH, {"src/index.js:1:7 error 'unused' is assigned a value but never used no-unused-vars",
                      "src/util.js:10:3 error Missing semicolon semi"}),
    ("src/a.ts:12:5: 'x' is unused [Error/no-unused-vars]\n",
     {"src/a.ts:12:5 'x' is unused [Error/no-unused-vars]"}),
    ("/work/app/src/a.ts: line 12, col 5, Error - 'x' is unused (no-unused-vars)\n",
     {"src/a.ts:12:5 error 'x' is unused (no-unused-vars)"}),
    (ISTANBUL, {"Coverage for lines (61.5%) does not meet global threshold (80%)"}),
    (MOCHA, {"mocha: Cart"}),
    (DENO, {"adds two ... FAILED", "adds two => ./cart_test.ts:3:6",
            "main.ts:1:7 error[no-unused-vars]: `x` is never used"}),
    ("ERROR in src/app/app.module.ts:4:21 - Cannot find module './x'\n",
     {"ERROR in src/app/app.module.ts:4:21 - Cannot find module './x'"}),
    # Counts, a bare assertion and a package manager's own lines name no failure: cannot tell.
    ("2 tests failed\nAssertionError: 2 != 3\nnpm error code 1\nerror Command failed with exit code 1.\n", set()),
    ("", set()),
])
def test_a_check_s_failing_lines_are_read_from_each_runner_s_own_shape(output, expected):
    assert check_failures(output, roots=("/work/app",)) == frozenset(expected)


def test_two_runs_of_the_same_failure_read_the_same_wherever_and_however_long_they_ran():
    here = "\x1b[31mFAIL\x1b[0m /tmp/w/task-3/src/a.test.ts (1.2 s)\n  ✕ adds (31 ms)\n"
    there = "FAIL /home/u/.cauce/worktrees/base-abc/src/a.test.ts (9.9 s)\n  ✕ adds (2 ms)\n"
    assert check_failures(here, roots=("/tmp/w/task-3",)) == check_failures(
        there, roots=("/home/u/.cauce/worktrees/base-abc/",)) == {"FAIL src/a.test.ts", "✕ adds"}
    many = "\n".join(f"FAIL src/t{i}.test.ts" for i in range(launch.FAILURES_KEPT + 20))
    assert len(check_failures(many)) == launch.FAILURES_KEPT
    long = check_failures("FAIL src/" + "x" * 600 + ".test.ts")
    assert all(len(line) <= launch.FAILURE_CHARS and line.endswith("…") for line in long)


# --- the comparison ---------------------------------------------------------------------

A, B, C = "FAIL a.test.ts", "FAIL b.test.ts", "FAIL c.test.ts"


@pytest.mark.parametrize(("work", "base", "must_fix", "before"), [
    ({A, B}, {A, B}, False, True),       # the same failures: there before
    ({A}, {A, B}, False, True),          # it fixed one and added none
    ({A, C}, {A, B}, False, False),      # it added one
    ({A}, None, False, False),           # the base passed, or could not tell
    (set(), {A}, False, False),          # no failing line read from the work's run: cannot tell
    ({A}, set(), False, False),          # none from the base's
    ({A, B}, {A, B}, True, False),       # a fix whose failures are all still there
    ({A}, {A, B}, True, True),           # a fix that made at least one go away
])
def test_a_failure_was_there_before_only_when_every_failing_line_of_the_work_failed_at_the_base(
        work, base, must_fix, before):
    assert launch.preexisting(frozenset(work), None if base is None else frozenset(base),
                              must_fix=must_fix) is before


# --- the base's own worktree ------------------------------------------------------------

def test_the_check_runs_at_the_base_in_a_worktree_of_its_own_that_is_always_removed(git_repo, tmp_path):
    (git_repo / ".gitignore").write_text("node_modules/\n")
    (git_repo / "package-lock.json").write_text('{"v": 1}\n')
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "deps")
    base = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "node_modules").mkdir()
    (git_repo / "node_modules" / "marker").write_text("installed\n")
    (git_repo / "app.py").write_text("print('changed')\n")
    git(git_repo, "commit", "-qam", "later")
    root = tmp_path / "home"
    with isolate.at_base(git_repo, base, root) as path:
        assert (path / "app.py").read_text() == "print('hi')\n"
        assert (path / "node_modules").is_symlink() and (path / "node_modules" / "marker").exists()
        assert git(path, "rev-parse", "HEAD") == base
    assert not path.exists() and (git_repo / "node_modules" / "marker").exists()
    assert str(path) not in git(git_repo, "worktree", "list")
    # A lockfile that moved since the base: what is installed is not what the base needs.
    (git_repo / "package-lock.json").write_text('{"v": 2}\n')
    git(git_repo, "commit", "-qam", "new deps")
    with pytest.raises(RuntimeError), isolate.at_base(git_repo, base, root) as path:
        assert not (path / "node_modules").exists()
        raise RuntimeError("the check broke")
    assert not path.exists() and git(git_repo, "worktree", "list").count("\n") == 0
    with pytest.raises(isolate.IsolationError), isolate.at_base(git_repo, "f" * 40, root):
        pass


def test_the_base_s_run_is_kept_for_a_day_per_repository_base_and_command(git_repo, store, tmp_path):
    counter = tmp_path / "runs.txt"
    check = f"echo x >> {counter}; echo 'FAIL old.test.js'; exit 1"
    base = git(git_repo, "rev-parse", "HEAD")
    baseline = orchestrate.Baseline(store, 1, git_repo, base, tmp_path / "home")
    first = baseline.at_base(check)
    assert first["code"] == 1 and first["failures"] == ["FAIL old.test.js"] and not first["cannot"]
    assert baseline.at_base(check)["failures"] == first["failures"]
    other = orchestrate.Baseline(store, 2, git_repo, base, tmp_path / "home")
    other.at_base(check)  # a sibling part on the same base reads the same run
    assert counter.read_text().count("x") == 1
    other.at_base(check + " # another command")
    assert counter.read_text().count("x") == 2
    # A day later it runs again.
    for key in (k for k in _meta_keys(store) if k.startswith(orchestrate.BASE_RUN_META)):
        kept = json.loads(store.meta(key))
        store.set_meta(key, json.dumps({**kept, "at": time.time() - orchestrate.BASE_RUN_KEPT_S - 1}))
    baseline.at_base(check)
    assert counter.read_text().count("x") == 3
    # What expired is dropped when a new run is kept: only the fresh one is left.
    assert len([k for k in _meta_keys(store) if k.startswith(orchestrate.BASE_RUN_META)]) == 1


def test_a_race_s_try_compares_with_the_base_from_its_own_thread(git_repo, store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    baseline = orchestrate.Baseline(store, 1, git_repo, git(git_repo, "rev-parse", "HEAD"), tmp_path / "home")
    with ThreadPoolExecutor(max_workers=2) as pool:
        got = list(pool.map(lambda _: baseline.at_base("echo 'FAIL old.test.js'; exit 1"), range(2)))
    assert [g["failures"] for g in got] == [["FAIL old.test.js"]] * 2
    assert any(k.startswith(orchestrate.BASE_RUN_META) for k in _meta_keys(store))


def _meta_keys(store: Store) -> list[str]:
    return [r[0] for r in store._conn.execute("SELECT key FROM meta")]


def test_a_base_that_cannot_run_the_check_cannot_tell(git_repo, store, tmp_path):
    base = git(git_repo, "rev-parse", "HEAD")
    baseline = orchestrate.Baseline(store, 1, git_repo, base, tmp_path / "home")
    assert "exit 127" in baseline.at_base("no-such-tool-anywhere")["cannot"]
    gone = orchestrate.Baseline(store, 1, git_repo, "e" * 40, tmp_path / "home")
    assert "no worktree could be made at the base" in gone.at_base("true")["cannot"]


# --- a red check on the work ------------------------------------------------------------

def _first(store: Store, report) -> dict:
    return store.attempts(report.task_id)[0]


def test_a_check_that_fails_at_the_base_with_the_same_lines_lets_the_pass_stand(git_repo, store):
    check = "echo 'FAIL legacy/old.test.js'; echo 'Jest: \"legacy/old.js\" coverage threshold for lines (80%) " \
            "not met: 41%'; exit 1"
    report = run("add the export button", git_repo, store, Options(verify=check), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    assert report.status == "done" and report.cells == ["sonnet/medium"]
    kept = store.last_event(report.task_id, "check_preexisting")["data"]
    assert kept["command"] == check and kept["n"] == kept["base_n"] == 2 and kept["added"] == 0
    assert kept["base"] == git(git_repo, "rev-parse", "HEAD") and "FAIL legacy/old.test.js" in kept["failures"]
    assert f"`{check}` fails at the base too, with the same 2 failures: they were there before this work, " \
           "not caused by it" in report.text()
    verdict = store.final_event(report.task_id)["data"]["verdict"]
    assert verdict["check"]["state"] == orchestrate.FAILING_AT_BASE and verdict["check"]["failures"] == 2
    said = "\n".join(hooks.verdict_lines(verdict))
    assert "failing at base, unchanged" in said and "passed, run by cauce" not in said
    # The ending offers the bugfix for them, and starts nothing.
    found = hooks._found_before(store, store.get_task(report.task_id))
    assert found[0].startswith("  it found 1 problem(s) that were already there")
    assert "cauce workflow run bugfix" in found[1] and f"--after task:{report.task_id}" in found[1]
    assert "FAIL legacy/old.test.js" in found[1]


def test_a_worker_that_stops_only_on_failures_already_at_the_base_passes(git_repo, store):
    from cauce.escalate import Failure

    check = "echo 'FAILED test_legacy.py::test_legacy_broken'; exit 1"
    stopped = WorkerResult(False, Failure.SPEC_BUG, f"Fixed mul. `{check}` still fails on test_legacy.py, which "
                           "the task forbids touching.", evidence="test_calc.py passes", cost_usd=0.1)
    report = run("fix mul so it returns the product", git_repo, store, Options(verify=check), registry={},
                 launcher=Script(stopped, write="f.py"), classifier=kind("implement"))
    assert report.status == "done" and len(store.attempts(report.task_id)) == 1
    assert store.last_event(report.task_id, "check_preexisting") is not None
    assert "no decision is left" in store.attempts(report.task_id)[0]["summary"]


def test_a_worker_s_open_point_about_something_else_stays_a_decision(git_repo, store):
    from cauce.escalate import Failure

    check = "echo 'FAILED test_legacy.py::test_legacy_broken'; exit 1"
    stopped = WorkerResult(False, Failure.SPEC_BUG, "Built it, but the request says two things about the "
                           "rounding: which one wins?", evidence="it builds", cost_usd=0.1)
    report = run("add the export button", git_repo, store, Options(verify=check), registry={},
                 launcher=Script(stopped, write="f.py"), classifier=kind("implement"))
    assert report.status == "blocked" and "decision" in report.text()


def test_a_check_whose_work_adds_a_failing_line_is_the_work_s_and_says_which_is_new(git_repo, store):
    check = "echo 'FAIL legacy/old.test.js'; if [ -f f.py ]; then echo 'FAIL src/f.test.js'; fi; exit 1"
    report = run("add the export button", git_repo, store, Options(verify=check, max_attempts=1), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    first = _first(store, report)
    assert first["failure"] == "code_bug" and report.status != "done"
    assert "1 failing line(s) are new with this work, focus on them:\n  - FAIL src/f.test.js" in first["evidence"]
    assert "1 already failed at the base, before this work" in first["evidence"]
    assert store.last_event(report.task_id, "check_preexisting") is None


def test_a_check_that_passes_at_the_base_is_the_work_s_failure(git_repo, store):
    check = "if [ -f f.py ]; then echo 'FAIL src/f.test.js'; exit 1; fi"
    report = run("add the export button", git_repo, store, Options(verify=check, max_attempts=1), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    first = _first(store, report)
    assert first["failure"] == "code_bug"
    assert "the same check passes: every failure here is this work's" in first["evidence"]


def test_a_check_whose_failures_cannot_be_read_is_the_work_s_failure(git_repo, store):
    report = run("add the export button", git_repo, store, Options(verify="echo '2 tests failed'; exit 1",
                                                                  max_attempts=1),
                 registry={}, launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    first = _first(store, report)
    assert first["failure"] == "code_bug" and "could not read which tests or files failed" in first["evidence"]


def test_a_base_that_cannot_run_the_check_leaves_the_failure_the_work_s(git_repo, store):
    # The tool the check needs exists only in the work: at the base the shell does not find it.
    check = "if [ -f f.py ]; then echo 'FAIL src/f.test.js'; exit 1; else exit 127; fi"
    report = run("add the export button", git_repo, store, Options(verify=check, max_attempts=1), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    first = _first(store, report)
    assert first["failure"] == "code_bug" and "the same check could not tell (exit 127" in first["evidence"]


def test_a_fix_must_make_at_least_one_of_the_failures_go_away(git_repo, store):
    same = "echo 'FAIL src/total.test.js'; exit 1"
    report = run("fix the total", git_repo, store, Options(verify=same, max_attempts=1), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("debug-repro"))
    first = _first(store, report)
    assert first["failure"] == "code_bug" and "none of them is gone yet" in first["evidence"]
    fixed_one = "echo 'FAIL src/total.test.js'; [ -f f.py ] || echo 'FAIL src/tax.test.js'; exit 1"
    report = run("fix the tax", git_repo, store, Options(verify=fixed_one), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("debug-repro"))
    assert report.status == "done"
    kept = store.last_event(report.task_id, "check_preexisting")["data"]
    assert kept["fixed"] == 1 and "and 1 more that failed at the base pass now" in report.text()


def test_a_worker_out_of_turns_whose_check_fails_only_as_at_the_base_is_settled(git_repo, store):
    out = WorkerResult(False, orchestrate.Failure.TURNS_EXHAUSTED, "used every turn", cost_usd=0.1)
    report = run("remove the old module", git_repo, store, Options(verify="echo 'FAIL old.test.js'; exit 1"),
                 registry={}, launcher=Script(out, write="gone.txt"), classifier=kind("refactor"))
    assert report.status == "done" and "failed only as it does at the base" in report.summary
    assert store.last_event(report.task_id, "check_preexisting") is not None


def test_a_clean_join_whose_check_fails_only_as_at_the_base_needs_no_worker(git_repo, store):
    from .test_orchestrate import JOIN, _two_branches

    base = _two_branches(git_repo, conflict=False)
    old = "echo 'FAIL legacy/old.test.js'; exit 1"
    report = run("Join the parts", git_repo, store, Options(kind="implement", base=base, integrate=JOIN, verify=old),
                 registry={}, launcher=lambda spec: pytest.fail("no worker for a join that only fails as before"),
                 classifier=kind("implement"), adapters=[])
    assert report.status == "done" and "fails on the joined work only as it does at the base" in report.summary
    assert "fails at the base too, with the same 1 failure:" in report.text()
    # One that adds a failure goes to a worker, told which line is new.
    told = []

    def fix(spec):
        told.append(spec.prompt)
        return ok("glued")

    new = "echo 'FAIL legacy/old.test.js'; [ -f api.py ] && echo 'FAIL src/api.test.js'; exit 1"
    run("Join the parts again", git_repo, store, Options(kind="implement", base=base, integrate=JOIN, verify=new,
                                                         max_attempts=1),
        registry={}, launcher=fix, classifier=kind("implement"), adapters=[])
    assert "are new with this work, focus on them:\n  - FAIL src/api.test.js" in told[0]


def test_the_comparison_is_left_out_without_a_base(tmp_path):
    plain = orchestrate._verified(ok(), "echo 'FAIL a.test.js'; exit 1", tmp_path)
    assert plain.failure is orchestrate.Failure.CODE_BUG and "base" not in plain.evidence
    assert orchestrate._against_base(None, "x", "FAIL a.test.js", tmp_path) == (False, "")
    assert orchestrate.preexisting_line(None) == ""


def test_the_doctor_knows_the_kinds_that_must_make_a_failure_go_away():
    assert doctor._MUST_FIX == orchestrate._REPRODUCES


def test_a_failure_called_pre_existing_that_the_work_added_is_suspect(store: Store):
    task = store.create_task("t", status="done", source="cauce", repo="r", kind="implement")
    fine = {"command": "npm test", "base": "abc", "n": 2, "base_n": 2, "fixed": 0, "added": 0}
    store.add_event(task["id"], "check_preexisting", **fine, version="0.34.0")
    assert not [f for f in doctor.check(store, days=0) if f.rule == "preexisting-that-was-not"]
    store.add_event(task["id"], "check_preexisting", **{**fine, "added": 1}, version="0.34.0")
    fix = store.create_task("f", status="done", source="cauce", repo="r", kind="debug-repro")
    store.add_event(fix["id"], "check_preexisting", **fine, version="0.34.0")
    store.add_event(fix["id"], "check_preexisting", **{**fine, "n": 3}, version="0.33.0")
    found = [f for f in doctor.check(store, days=0) if f.rule == "preexisting-that-was-not"]
    suspect = [f for f in found if f.severity == doctor.SUSPECT]
    assert {f.task_id for f in suspect} == {task["id"], fix["id"]}
    assert "1 new" in next(f.what for f in suspect if f.task_id == task["id"])
    assert [f.severity for f in found if f.severity == doctor.FIXED] == [doctor.FIXED]
    assert doctor.FIXED_IN["preexisting-that-was-not"] == "0.34.0"


# --- a check whose exit says nothing ----------------------------------------------------

@pytest.mark.parametrize(("command", "why"), [
    ("npm test | grep -v 'console.log'", "grep -v"),
    ("pytest -q 2>&1 | tail -20", "tail"),
    ("npm run lint && npm test | tee test.log", "tee"),
    ("jest | /usr/bin/head -50", "head"),
    ("npm test || true", "|| true"),
    ("npm test || :", "|| :"),
    ("make check; exit 0", "; exit 0"),
    ("go test ./... ; true", "; true"),
    ("set -o pipefail; npm test | tail -20", None),
    ("set -euo pipefail && pytest | tee out.txt", None),
    ("bash -c 'set -e -o pipefail; jest | tail'", None),
    ("npm test", None),
    ("npm test || npm run test:retry", None),
    ("echo 'a | grep b' && npm test", None),
    ("npm test | node report.js", None),
])
def test_a_check_whose_exit_is_a_filter_s_or_always_zero_is_read_as_weak(command, why):
    assert ci.weak_line(command) == why


def test_the_plan_warns_once_of_a_weak_check_and_reads_the_scripts_it_runs(git_repo, store):
    warned = ci.weak("npm test | grep -v warn")
    assert warned == ("this task's check `npm test | grep -v warn` exits as its last command (`grep -v`) does: a "
                      "failure before it never reaches cauce, so a green check proves nothing; add `set -o pipefail` "
                      "or drop the pipe")
    assert "always exits 0 (`|| true`)" in ci.weak("npm test || true")
    (git_repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest --ci | tee jest.log"}}))
    assert "(through a script it runs: `jest --ci | tee jest.log`)" in ci.weak("npm test", git_repo)
    assert ci.weak("pytest", git_repo) is None
    dry = run("add the export button", git_repo, store, Options(dry_run=True, verify="npm test"), registry={},
              classifier=kind("implement"))
    weak = [w for w in orchestrate.warnings(dry.plan.reasons) if w.startswith("this task's check")]
    assert len(weak) == 1 and "`tee`" in weak[0]


def test_the_check_card_without_a_base_failure_is_a_plain_pass():
    assert orchestrate._check_card("npm test", None) == {"command": "npm test", "passed": True}
