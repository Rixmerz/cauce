from __future__ import annotations

import contextlib
import json
import socket

import pytest

from cauce import cli, escalate, grants, preflight, repo, stops
from cauce.allow import from_refusals, rules
from cauce.escalate import Attempt, Failure, Move, decide
from cauce.launch import build_argv, parse
from cauce.matrix import LADDERS, MIN_ATTEMPT_USD, Cell
from cauce.orchestrate import Options, run, start_turns
from cauce.workflows import _step_budget

from .conftest import git
from .test_launch import block, envelope, proc, spec
from .test_orchestrate import Script, bad, kind, ok

SONNET = Cell("sonnet", "medium")


def _store_attempts(store, kind_name, cell, *, turns=(), costs=(), repo="r"):
    for i, (t, c) in enumerate(zip(turns or [10] * len(costs), costs or [0.5] * len(turns), strict=True)):
        task = store.create_task(f"t{i}", status="done", source="cauce", repo=repo, kind=kind_name)
        store.add_attempt(task["id"], cell=cell.label, max_turns=30, passed=1, failure=None, summary="ok",
                          evidence="", cost_usd=c, input_tokens=0, output_tokens=0, turns=t, duration_s=0,
                          changed_paths=(), served_model=None, capabilities=())
        store.update_task(task["id"], cost_usd=c, final_cell=cell.label)


# --- the check's programs ---------------------------------------------------------


def test_a_check_whose_program_is_missing_blocks_and_one_that_exists_does_not(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    found = preflight.verify_findings("no-such-tool-xyz --run && git status", tmp_path, env)
    assert [f.kind for f in found] == ["blocker"]
    assert "`no-such-tool-xyz` is not on the worker's PATH" in found[0].what
    assert preflight.verify_findings("git status; test -f x || { echo no; exit 1; }", tmp_path, env) == []
    assert preflight.verify_findings(None, tmp_path, env) == []
    missing = preflight.verify_findings(".venv/bin/python -m pytest", tmp_path, env)
    assert "does not exist" in missing[0].what
    (tmp_path / "run.sh").write_text("#!/bin/sh\n")
    (tmp_path / "run.sh").chmod(0o755)
    assert preflight.verify_findings("./run.sh", tmp_path, env) == []


def test_an_npm_check_needs_its_manifest_and_its_script(tmp_path):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if preflight.shutil.which("npm", path=env["PATH"]) is None:
        pytest.skip("npm is not installed here")
    assert "no package.json" in preflight.verify_findings("npm run build", tmp_path, env)[0].what
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    found = preflight.verify_findings("npm run build", tmp_path, env)
    assert "no script 'build'" in found[0].what and "scripts: test" in found[0].fix
    assert preflight.verify_findings("npm test", tmp_path, env) == []
    assert preflight.verify_findings("cd app && npm run build", tmp_path, env) == []  # cannot tell where


# --- the API -----------------------------------------------------------------------


class Refuse:
    def __init__(self, error: Exception | None, then_ok_after: int | None = None):
        self.error, self.calls, self.after = error, [], then_ok_after

    def __call__(self, address, timeout):
        self.calls.append(address)
        if self.error is not None and (self.after is None or len(self.calls) <= self.after):
            raise self.error
        return contextlib.nullcontext()


def test_the_api_endpoint_follows_the_base_url_and_the_proxy():
    assert preflight.api_endpoint({}) == ("api.anthropic.com", 443)
    assert preflight.api_endpoint({"ANTHROPIC_BASE_URL": "http://gateway.internal:8080/v1"}) == ("gateway.internal",
                                                                                                8080)
    assert preflight.api_endpoint({"HTTPS_PROXY": "http://proxy.local:3128"}) == ("proxy.local", 3128)
    assert preflight.api_endpoint({"HTTPS_PROXY": "proxy.local:3128", "NO_PROXY": "anthropic.com"}) == (
        "api.anthropic.com", 443)
    assert preflight.api_endpoint({"CLAUDE_CODE_USE_BEDROCK": "1"}) is None


def test_an_unreachable_api_is_waited_for_with_backoff_then_stops_without_an_attempt():
    on = {"CAUCE_API_CHECK": "on"}
    slept: list[float] = []
    dns = Refuse(socket.gaierror(8, "nodename nor servname provided"))
    found = preflight.wait_for_api(on, wait_s=30, connect=dns, sleep=slept.append)
    assert found is not None and found.blocks and found.cause == "environment"
    assert "does not resolve" in found.why and sum(slept) == 30 and slept == [5.0, 10.0, 15.0]
    back = Refuse(ConnectionResetError("reset"), then_ok_after=2)
    slept.clear()
    assert preflight.wait_for_api(on, wait_s=30, connect=back, sleep=slept.append) is None
    assert slept == [5.0, 10.0]
    assert preflight.wait_for_api({"CAUCE_API_CHECK": "off"}, connect=dns, sleep=slept.append) is None
    assert preflight.api_finding(on, Refuse(None)) is None


def test_a_run_whose_api_never_answers_is_blocked_as_environment_and_spends_nothing(git_repo, store, monkeypatch):
    gone = preflight.Finding("blocker", "api", "the API could not be reached for 180s", "x does not resolve",
                             "check the network", cause="environment")
    monkeypatch.setattr(preflight, "wait_for_api", lambda **kw: gone)
    script = Script(ok())
    report = run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and script.specs == [] and report.cost_usd == 0
    assert report.stop.cause == "environment" and report.stop.reason.startswith("before any attempt")
    assert store.attempts(report.task_id) == []


def test_a_retry_after_a_broken_launch_waits_for_the_api_instead_of_spending_itself(git_repo, store, monkeypatch):
    answers = [None, preflight.Finding("blocker", "api", "down", "", "check the network", cause="environment")]
    monkeypatch.setattr(preflight, "wait_for_api", lambda **kw: answers.pop(0))
    broke = bad(Failure.ENVIRONMENT, "the session errored: ENOTFOUND")
    broke = type(broke)(**{**broke.__dict__, "transient": True})
    script = Script(broke, ok())
    report = run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == 1 and report.status == "blocked" and report.stop.cause == "environment"
    assert report.stop.attempt == 1


# --- budget ------------------------------------------------------------------------


def test_a_budget_below_one_attempt_stops_before_launching_and_names_the_budget(git_repo, store):
    script = Script(ok())
    report = run("add the export", git_repo, store, Options(budget_usd=0.09), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert script.specs == [] and report.status == "blocked" and report.stop.cause == "budget"
    assert stops.next_step(report.task_id, report.stop) == f"cauce resume {report.task_id} --budget 0.25"
    assert stops.resume_args(report.task_id, stops.view(store, store.get_task(report.task_id)))[-2:] == [
        "--budget", "0.25"]


def test_before_every_attempt_the_budget_left_must_pay_for_the_next_cell(git_repo, store):
    script = Script(bad(Failure.CODE_BUG, cost=4.9), ok())
    report = run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert len(script.specs) == 1 and report.stop.cause == "budget" and report.stop.attempt == 1
    assert report.stop.extra["needed_usd"] == 0.25 and "needs at least $0.25" in report.stop.reason


def test_the_minimum_attempt_and_the_estimate_come_from_history(store):
    assert preflight.minimum_attempt(store, "implement", SONNET) == (MIN_ATTEMPT_USD["sonnet"],
                                                                    "cauce's floor for an attempt on sonnet")
    _store_attempts(store, "implement", SONNET, costs=[0.4, 0.6, 0.8, 1.0, 1.2])
    _store_attempts(store, "implement", Cell("sonnet", "high"), costs=[2.0] * 5)
    low, why = preflight.minimum_attempt(store, "implement", SONNET)
    assert low == 0.6 and "5 passing implement attempts at sonnet/medium" in why
    found = preflight.budget_findings(store, "implement", LADDERS["implement"], SONNET, 1.0, "r")
    assert [f.check for f in found] == ["estimate"] and found[0].kind == "warning"
    assert "pass --budget 3 " in found[0].fix and "cost $1.40 on average" in found[0].why
    assert preflight.budget_findings(store, "implement", LADDERS["implement"], SONNET, 3.0, "r") == []
    assert preflight.budget_findings(store, "implement", LADDERS["implement"], SONNET, 0.5, "r")[0].cause == "budget"
    assert preflight.estimate(store, "implement", LADDERS["implement"], "r") == 3.0
    assert preflight.estimate(store, "docs", LADDERS["docs"]) is None
    assert preflight.dollars(0.26) == 0.5 and preflight.dollars(0.25) == 0.25


def test_a_workflow_step_without_a_budget_gets_one_from_its_kind(store):
    assert _step_budget(store, {"kind": "implement"}, "r") is None
    _store_attempts(store, "implement", SONNET, costs=[3.0] * 5)
    assert _step_budget(store, {"kind": "implement"}, "r") == 3.25
    assert _step_budget(store, {}, "r") is None


# --- the base the task names ---------------------------------------------------------


@pytest.mark.parametrize(("text", "name"), [
    ("Build it from branch release/2.1", "release/2.1"),
    ("work on branch feat-x and add the route", "feat-x"),
    ("start from the develop branch", "develop"),
    ("rebuild the cache at 3fa4b21c", "3fa4b21c"),
    ("take it from `hotfix`.", "hotfix"),
    ("port the fix from main", None),
    ("work from the same branch as before", None),
    ("refactor at deadbeef", None),
    ("on branch X", None),
])
def test_the_base_a_task_names(text, name):
    assert preflight.named_base(text) == name


def test_a_named_base_starts_the_worktree_and_one_that_does_not_resolve_blocks(git_repo, store):
    git(git_repo, "checkout", "-q", "-b", "develop")
    (git_repo / "only-on-develop.txt").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "develop")
    git(git_repo, "checkout", "-q", "main")
    seen = []

    def launcher(s):
        seen.append((s.target_dir / "only-on-develop.txt").exists())
        return ok()

    run("add a route, starting from the develop branch", git_repo, store, registry={}, launcher=launcher,
        classifier=kind("implement"))
    assert seen == [True]
    script = Script(ok())
    report = run("add a route on branch nowhere-yet", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert script.specs == [] and report.stop.cause == "preflight" and "'nowhere-yet'" in report.stop.reason
    assert stops.next_step(report.task_id, report.stop) == f"cauce resume {report.task_id}"


# --- inputs a worktree would not have ---------------------------------------------------


def test_inputs_git_does_not_have_are_a_warning_naming_no_isolate(git_repo):
    (git_repo / ".gitignore").write_text("data/\n")
    (git_repo / "data").mkdir()
    (git_repo / "data" / "rates.csv").write_text("x\n")
    (git_repo / "notes.md").write_text("x\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-q", "-m", "ignore")
    found = preflight.ignored_inputs("convert data/rates.csv with app.py, see notes.md and node_modules/x", git_repo)
    assert len(found) == 1 and found[0].kind == "warning" and "--no-isolate" in found[0].fix
    assert found[0].data["paths"] == ["data/rates.csv", "notes.md"]
    assert "data/rates.csv (ignored)" in found[0].what and "notes.md (untracked)" in found[0].what
    assert preflight.ignored_inputs("fix app.py", git_repo) == []


# --- requirements earlier tasks stopped on ------------------------------------------------


def test_what_a_worker_names_missing_becomes_probes():
    account = ("zsh: command not found: pandoc\nModuleNotFoundError: No module named 'openpyxl.styles'\n"
               "connect ECONNREFUSED 127.0.0.1:5432; it: command not found")
    assert preflight.missing_from(account) == [("program", "pandoc"), ("module", "openpyxl"), ("port", "5432")]
    assert preflight.probe("program", "git", {"PATH": "/usr/bin:/bin"})
    assert not preflight.probe("program", "no-such-tool-xyz", {"PATH": "/usr/bin:/bin"})
    assert preflight.probe("module", "json", {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    assert not preflight.probe("module", "no_such_module_xyz", {"PATH": "/usr/bin:/bin"})
    assert not preflight.probe("module", "os; import sys", {"PATH": "/usr/bin:/bin"})
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert preflight.probe("port", str(server.getsockname()[1]), {})


def test_an_environment_stop_keeps_a_requirement_the_next_similar_task_is_checked_for(git_repo, store,
                                                                                         monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")
    named = bad(Failure.ENVIRONMENT, "the converter needs no-such-converter-xyz: zsh: command not found: "
                                     "no-such-converter-xyz")
    first = run("convert the quarterly spreadsheet to markdown", git_repo, store, registry={},
                launcher=Script(named), classifier=kind("implement"))
    assert first.stop.cause == "environment"
    kept = store.notes(repo.key(git_repo), topics=["environment"])
    assert len(kept) == 1 and "probe: program no-such-converter-xyz" in kept[0]["text"]
    assert any("requirements: kept" in line for line in first.impact)
    script = Script(ok())
    second = run("convert the yearly spreadsheet to markdown", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"))
    assert script.specs == [] and second.stop.cause == "preflight" and "command -v no-such-converter-xyz" in (
        second.stop.reason)
    other = Script(ok())
    assert run("rename the login button", git_repo, store, registry={}, launcher=other,
               classifier=kind("implement")).status == "done"


def test_a_requirement_whose_probe_passes_is_cleared(store, tmp_path):
    from cauce import notes

    note_id, _ = notes.add(store, "r", "Tasks here need command -v git: task #1 stopped without it (convert the "
                           "spreadsheet). probe: program git", topic="environment", author="worker",
                           filed_by="probe")
    assert preflight.requirement_findings(store, "r", "convert the spreadsheet", {"PATH": "/usr/bin:/bin"}) == []
    assert store.notes("r", ids=[note_id])[0]["state"] == "dropped"
    kept = preflight.learn(store, "r", {"id": 7, "title": "x"}, "bash: git: command not found", {},
                           check_now=lambda *a: True)
    assert kept == []


# --- what a one-shot worker does not have -------------------------------------------------


def test_a_login_with_no_test_account_recorded_is_a_warning(store):
    from cauce import notes

    found = preflight.unmet("log in as an agent and check the bookings page", {}, store, "r")
    assert [f.check for f in found] == ["login"] and found[0].kind == "warning"
    notes.add(store, "r", "The staging test account is read from the E2E_USER variable", topic="environment",
              author="person", filed_by="person")
    assert preflight.unmet("log in as an agent and check the bookings page", {}, store, "r") == []


def test_dry_run_and_the_preflight_command_print_the_findings(git_repo, store, capsys):
    dry = run("add the export", git_repo, store, Options(dry_run=True, verify="no-such-tool-xyz"), registry={},
              classifier=kind("implement"))
    assert "preflight blocker: the check `no-such-tool-xyz` cannot run" in dry.text()
    assert cli.main(["preflight", "add the export", "--repo", str(git_repo), "--verify", "no-such-tool-xyz",
                     "--kind", "implement"]) == 1
    out = capsys.readouterr().out
    assert "blocker: the check `no-such-tool-xyz` cannot run" in out
    assert cli.main(["preflight", "add the export", "--repo", str(git_repo), "--kind", "implement"]) == 0
    assert "nothing found that would stop a worker" in capsys.readouterr().out


# --- turns ---------------------------------------------------------------------------------


def test_the_first_attempt_s_turns_come_from_what_passing_attempts_used(store):
    assert start_turns(store, "implement", "r") == (30, "starts with 30 turns: the kind's default, with too few "
                                                        "passing attempts to size it")
    _store_attempts(store, "implement", SONNET, turns=[20, 40, 50, 60, 70])
    turns, why = start_turns(store, "implement", "r")
    assert turns == 60 and "80% of the last 5 passing implement attempts in this repository used 60" in why
    assert start_turns(store, "implement", "elsewhere")[0] == 60  # all repositories
    _store_attempts(store, "test", SONNET, turns=[5] * 5)
    assert start_turns(store, "test", "r")[0] == 30
    _store_attempts(store, "ui", SONNET, turns=[400] * 5)
    turns, why = start_turns(store, "ui", "r")
    assert turns == 120 and "capped at 120" in why


def test_a_run_out_of_turns_continues_its_own_session(git_repo, store):
    out = bad(Failure.TURNS_EXHAUSTED, "used all 30 turns")
    out = type(out)(**{**out.__dict__, "session_id": "s-1"})
    script = Script(out, ok())
    report = run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    first, second = script.specs
    assert first.persist_session and first.resume_session is None
    assert second.resume_session == "s-1" and second.max_turns == 60
    assert second.prompt.startswith("You ran out of turns before you finished")
    assert build_argv(second)[build_argv(second).index("--resume") + 1] == "s-1"
    assert "--no-session-persistence" not in build_argv(first)
    finished = store.last_event(report.task_id, "attempt_finished")
    assert finished is not None


def test_a_resume_of_a_task_that_outgrew_its_turns_continues_the_last_session(git_repo, store, monkeypatch):
    out = bad(Failure.TURNS_EXHAUSTED, "used all 30 turns")
    out = type(out)(**{**out.__dict__, "session_id": "s-9"})
    report = run("add the export", git_repo, store, Options(max_attempts=2), registry={},
                 launcher=Script(out, out), classifier=kind("implement"))
    assert report.stop.cause == "turns"
    seen = []
    monkeypatch.setattr("cauce.orchestrate.launch.run", lambda s: seen.append(s) or ok())
    monkeypatch.chdir(git_repo)
    assert cli.main(["resume", str(report.task_id), "--no-model"]) == 0
    assert seen[0].resume_session == "s-9" and seen[0].max_turns == stops.RESUME_TURNS


# --- hooks, read-only git, shared refs ---------------------------------------------------------


def test_a_hook_s_refusal_is_no_permission_and_suggests_no_rule(tmp_path):
    text = ("A PreToolUse hook blocked `git add -A`: Sonar finds 2 blockers, fix them first.\n"
            + block(verdict="inconclusive", summary="git add was refused by a hook", evidence=""))
    entry = {"tool_name": "Bash", "tool_input": {"command": "git add -A"}, "tool_use_id": "t1"}
    out = parse(proc(envelope(text, permission_denials=[entry])), spec(tmp_path))
    assert out.failure is Failure.HOOK and out.denied == () and out.allow == ()
    assert out.hooked == ("Bash(git add -A)",) and "Sonar finds 2 blockers" in out.hook_message
    marked = {**entry, "decision_reason": "PreToolUse hook: no commits on Fridays"}
    out = parse(proc(envelope(block(verdict="inconclusive", summary="s", evidence=""),
                              permission_denials=[marked, {**entry, "tool_input": {"command": "npm run build"}}])),
                spec(tmp_path))
    assert out.failure is Failure.PERMISSION and out.denied == ("Bash(npm run build)",)
    assert out.hooked == ("Bash(git add -A)",) and "no commits on Fridays" in out.hook_message
    assert out.allow == ("Bash(npm run build:*)",)


def test_a_hook_refusal_blocks_with_the_hook_s_message_and_no_allow(git_repo, store):
    hooked = bad(Failure.HOOK, "a hook refused git add")
    hooked = type(hooked)(**{**hooked.__dict__, "hooked": ("Bash(git add -A)",), "hook_message": "fix Sonar first"})
    decision = decide(LADDERS["implement"], [Attempt(SONNET, 30, False, Failure.HOOK, hooked=("Bash(git add -A)",),
                                                     hook_message="fix Sonar first")])
    assert decision.move is Move.BLOCKED and "fix Sonar first" in decision.reason and "--allow" not in decision.reason
    report = run("commit the fix", git_repo, store, registry={}, launcher=Script(hooked), classifier=kind("implement"))
    assert report.status == "blocked" and report.stop.cause == "hook" and report.stop.who == "hook"
    assert stops.next_step(report.task_id, report.stop) == f"cauce resume {report.task_id}"
    text = report.text()
    assert "stopped by a hook you installed" in text and "cauce allow" not in text
    assert "fix Sonar first" in report.stop.reason
    assert escalate.MEANING[Failure.HOOK]


def test_workers_may_read_git_and_never_move_a_shared_ref(git_repo, store):
    script = Script(ok())
    run("add the export", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    s = script.specs[0]
    assert set(grants.READ_GIT) <= set(s.allowed_tools)
    assert "Bash(git reset:*)" in s.disallowed_tools and "Bash(git checkout:*)" not in s.disallowed_tools
    argv = build_argv(s)
    assert argv[argv.index("--disallowedTools") + 1] == "Bash(git reset:*)"  # a rule with spaces goes alone
    in_place = Script(ok())
    run("add the import", git_repo, store, Options(isolate=False), registry={}, launcher=in_place,
        classifier=kind("implement"))
    assert {"Bash(git checkout:*)", "Bash(git rebase:*)"} <= set(in_place.specs[0].disallowed_tools)


@pytest.mark.parametrize(("rule", "refused"), [
    ("Bash(git reset:*)", True), ("Bash(git reset --hard HEAD~1)", True), ("Bash(git branch -D:*)", True),
    ("Bash(git push --force:*)", True), ("Bash(git update-ref:*)", True), ("Bash(git checkout:*)", True),
    ("Bash(git:*)", False), ("Bash(git branch --list:*)", False), ("Bash(git push:*)", False), ("Read", False),
])
def test_rules_that_rewrite_shared_refs_are_never_granted(rule, refused):
    assert (grants.never(rule) is not None) is refused


def test_allow_never_suggests_and_cauce_allow_never_keeps_a_shared_ref_rule(git_repo, capsys):
    assert rules("Bash", {"command": "git reset --hard origin/main"}) == []
    assert rules("Bash", {"command": "git status && git reset --hard"}) == ["Bash(git status:*)"]
    assert from_refusals(["Bash(git reset --hard HEAD)"]) == ()
    assert cli.main(["allow", "--repo", str(git_repo), "Bash(git reset:*)", "Bash(npm:*)"]) == 1
    captured = capsys.readouterr()
    assert "not kept: Bash(git reset:*) is never granted to a worker" in captured.err
    assert "Bash(npm:*)" in captured.out and "git reset" not in captured.out


def test_a_preflight_stop_is_recorded_and_its_account_read_back(git_repo, store):
    report = run("add the export", git_repo, store, Options(verify="no-such-tool-xyz"), registry={},
                 launcher=Script(ok()), classifier=kind("implement"))
    finished = store.last_event(report.task_id, "finished")["data"]["stop"]
    assert finished["cause"] == "preflight" and finished["preflight"]["check"] == "verify"
    shown = stops.view(store, store.get_task(report.task_id))
    assert shown["who"] == "cauce's rules" and "environment would stop the worker" in shown["todo"]
    assert store.last_event(report.task_id, "preflight")["data"]["findings"][0]["kind"] == "blocker"
    assert stops.resume_label("hook") == "Resume"

