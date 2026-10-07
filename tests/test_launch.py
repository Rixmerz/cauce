from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cauce import launch
from cauce.escalate import Failure
from cauce.launch import LaunchSpec, build_argv, parse, result_block
from cauce.matrix import Cell

from .conftest import git


def spec(tmp_path: Path, **kw) -> LaunchSpec:
    return LaunchSpec(prompt="do it", cell=kw.pop("cell", Cell("sonnet", "high")), launch_dir=tmp_path, **kw)


def block(**fields) -> str:
    return "done.\n```cauce-result\n" + json.dumps(fields) + "\n```"


def envelope(text: str = "", **extra) -> str:
    return json.dumps({"is_error": False, "result": text, "total_cost_usd": 0.12, "num_turns": 4,
                       "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}, **extra})


def proc(stdout: str, code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], code, stdout, stderr)


def test_a_pinned_model_id_replaces_the_alias_and_the_served_model_is_read_from_the_cli(tmp_path):
    assert build_argv(spec(tmp_path))[build_argv(spec(tmp_path)).index("--model") + 1] == "sonnet"
    pinned = build_argv(spec(tmp_path, model_id="claude-sonnet-5-5"))
    assert pinned[pinned.index("--model") + 1] == "claude-sonnet-5-5"
    usage = {"claude-sonnet-5-5": {"outputTokens": 700}, "claude-haiku-4-5": {"outputTokens": 30}}
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed"), modelUsage=usage)),
                spec(tmp_path))
    assert out.served_model == "claude-sonnet-5-5"
    assert parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1"))), spec(tmp_path)).served_model == ""


def test_argv_carries_every_ceiling_and_restriction(tmp_path):
    work = tmp_path / "work"
    s = spec(tmp_path, workdir=work, max_turns=12, max_budget_usd=1.5, tools=("Read", "Bash"),
             disallowed_tools=("Edit", "Write"), append_system_prompt="hint")
    argv = build_argv(s, mcp_config_path=tmp_path / "m.json")
    def val(flag):
        return argv[argv.index(flag) + 1]
    assert val("--model") == "sonnet" and val("--effort") == "high"
    assert val("--max-turns") == "12" and val("--max-budget-usd") == "1.50"
    assert val("--tools") == "Read,Bash" and val("--disallowedTools") == "Edit,Write"
    assert val("--add-dir") == str(work) and val("--mcp-config") == str(tmp_path / "m.json")
    assert val("--append-system-prompt") == "hint"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv


def test_haiku_gets_no_effort_flag(tmp_path):
    argv = build_argv(spec(tmp_path, cell=Cell("haiku")))
    assert "--effort" not in argv and "--add-dir" not in argv and "--tools" not in argv


def test_a_pass_needs_evidence(tmp_path):
    s = spec(tmp_path)
    ok = parse(proc(envelope(block(verdict="pass", summary="fixed", evidence="3 passed"))), s)
    assert ok.passed and ok.evidence == "3 passed"
    assert (ok.cost_usd, ok.input_tokens, ok.output_tokens, ok.turns) == (0.12, 100, 7, 4)
    bare = parse(proc(envelope(block(verdict="pass", summary="fixed"))), s)
    assert not bare.passed and bare.failure is Failure.INCONCLUSIVE


@pytest.mark.parametrize(
    ("stdout", "failure"),
    [
        (envelope(block(verdict="fail", failure="approach", summary="wrong way")), Failure.APPROACH),
        (envelope(block(verdict="fail", failure="nonsense", summary="x")), Failure.CODE_BUG),
        (envelope(block(verdict="inconclusive", summary="x")), Failure.INCONCLUSIVE),
        (envelope(block(verdict="inconclusive", failure="spec_bug", summary="contradicts")), Failure.SPEC_BUG),
        (envelope(block(verdict="inconclusive", failure="code_bug", summary="x")), Failure.INCONCLUSIVE),
        (envelope("no block at all"), Failure.INCONCLUSIVE),
        (envelope(block(verdict="pass") + block(verdict="fail")), Failure.INCONCLUSIVE),
        (json.dumps({"is_error": True, "subtype": "error_max_turns"}), Failure.TURNS_EXHAUSTED),
        (json.dumps({"is_error": True, "subtype": "error_max_budget_usd"}), Failure.BUDGET_EXHAUSTED),
        (json.dumps({"is_error": True, "subtype": "error_during_execution"}), Failure.ENVIRONMENT),
        ("not json", Failure.ENVIRONMENT),
    ],
)
def test_every_failure_is_typed(tmp_path, stdout, failure):
    result = parse(proc(stdout), spec(tmp_path))
    assert not result.passed and result.failure is failure


def test_an_errored_session_says_why_not_its_subtype(tmp_path):
    limited = json.dumps({"is_error": True, "subtype": "success", "result": "Claude AI usage limit reached"})
    assert parse(proc(limited), spec(tmp_path)).summary == "the session errored: Claude AI usage limit reached"
    bare = json.dumps({"is_error": True, "subtype": "success"})
    assert parse(proc(bare), spec(tmp_path)).summary == "the session errored: success"
    crashed = json.dumps({"is_error": True, "subtype": "error_during_execution", "result": ""})
    assert parse(proc(crashed), spec(tmp_path)).summary == "the session errored: error_during_execution"


def test_result_block_rejects_garbage():
    assert result_block("```cauce-result\n[1]\n```") is None
    assert result_block("```cauce-result\n{bad\n```") is None


def test_run_reads_changes_from_git_and_cleans_its_mcp_file(git_repo):
    seen = {}

    def runner(argv, **kwargs):
        cfg = Path(argv[argv.index("--mcp-config") + 1])
        seen["mcp"] = json.loads(cfg.read_text())
        seen["cfg"] = cfg
        seen["input"] = kwargs["input"]
        seen["env"] = kwargs["env"]
        (git_repo / "new.py").write_text("x = 1\n")
        (git_repo / "app.py").write_text("print('changed')\n")
        git(git_repo, "add", "app.py")
        git(git_repo, "commit", "-q", "-m", "worker commit")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="ok")))

    s = LaunchSpec("task", Cell("sonnet", "low"), git_repo, mcp_servers={"idx": {"command": "idx"}})
    result = launch.run(s, runner=runner)
    assert result.passed
    assert result.changed_paths == ("app.py", "new.py")
    assert seen["mcp"] == {"mcpServers": {"idx": {"command": "idx"}}}
    assert not seen["cfg"].exists() and git_repo not in seen["cfg"].parents
    assert "cauce-result" in seen["input"] and seen["env"]["CAUCE_HOOKS_OFF"] == "1"


def test_run_timeout_and_missing_cli(tmp_path):
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    result = launch.run(spec(tmp_path, timeout_s=1), runner=slow)
    assert result.failure is Failure.ENVIRONMENT

    def missing(argv, **kwargs):
        raise FileNotFoundError("claude")

    with pytest.raises(launch.LaunchError):
        launch.run(spec(tmp_path), runner=missing)


def test_changes_outside_git_are_unknown(tmp_path):
    assert launch.tracked_state(tmp_path) is None
    assert launch.changed_paths(None, {}) == ()
    assert launch.describe(["a", "b" * 100]).endswith("...")


def test_granted_rules_reach_the_worker_one_argument_each(tmp_path):
    argv = build_argv(spec(tmp_path, allowed_tools=("Bash(npm run build)", "Bash(node:*)")))
    at = argv.index("--allowedTools")
    assert argv[at + 1:at + 3] == ["Bash(npm run build)", "Bash(node:*)"]
    assert "--allowedTools" not in build_argv(spec(tmp_path))
    argv = build_argv(spec(tmp_path, extra_dirs=(tmp_path / "node_modules",)))
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "node_modules")


def test_a_refused_command_is_read_from_the_cli_not_the_worker(tmp_path):
    s = spec(tmp_path)
    refusals = [
        {"tool_name": "Bash", "tool_input": {"command": "npm   run build", "description": "build"}},
        {"tool_name": "Bash", "tool_input": {"command": "npm run build"}},
        {"tool_name": "Write", "tool_input": {"file_path": "src/app.ts"}},
        {"tool_name": "WebFetch", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": {"command": "x" * 200}},
        "garbage", {"tool_input": {"command": "ls"}},
    ]
    said_done = block(verdict="inconclusive", failure="environment", summary="created the routes; build refused")
    result = parse(proc(envelope(said_done, permission_denials=refusals)), s)
    assert not result.passed and result.failure is Failure.PERMISSION and result.verdict == "inconclusive"
    assert result.denied[:3] == ("Bash(npm run build)", "Write(src/app.ts)", "WebFetch")
    assert len(result.denied) == 4 and result.denied[3].endswith("...)") and len(result.denied[3]) == 126
    assert result.allow == ("Bash(npm run build:*)", "Write(src/app.ts)", "WebFetch", f"Bash({'x' * 200}:*)")
    assert launch.allow_rules(None) == ()
    # no block at all, or a pass it could not show: the refusal still decides
    assert parse(proc(envelope("stopped", permission_denials=refusals[:1])), s).failure is Failure.PERMISSION
    bare = parse(proc(envelope(block(verdict="pass", summary="done"), permission_denials=refusals[:1])), s)
    assert bare.failure is Failure.PERMISSION and bare.verdict == "pass"
    # a task the worker found wrong is wrong whatever was refused
    wrong = block(verdict="fail", failure="spec_bug", summary="read-only task asks for files")
    assert parse(proc(envelope(wrong, permission_denials=refusals[:1])), s).failure is Failure.SPEC_BUG
    # a pass it could show stands: it found another way
    shown = block(verdict="pass", summary="done", evidence="ok")
    assert parse(proc(envelope(shown, permission_denials=refusals[:1])), s).passed
    assert launch.denials(None) == () and launch.denials({"permission_denials": None}) == ()


def test_work_in_checkouts_nested_in_the_work_directory_is_seen(tmp_path):
    """A task run in a folder that holds several repositories edited two of them;
    the folder is no checkout itself, and the report said it changed nothing."""
    from .conftest import git

    def checkout(path):
        path.mkdir(parents=True)
        git(path, "init", "-q")
        git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        return path

    holder = tmp_path / "workspace"
    api, web = checkout(holder / "api"), checkout(holder / "apps" / "web")
    (holder / "node_modules" / "dep").mkdir(parents=True)
    (holder / "node_modules" / "dep" / ".git").mkdir()  # never looked into
    assert launch.nested_checkouts(holder) == [api, web]
    before = launch.tracked_state(holder)
    (api / "routes.js").write_text("x\n")
    (web / "app.ts").write_text("y\n")
    git(web, "add", "app.ts")
    git(web, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "worker committed")
    after = launch.tracked_state(holder)
    assert launch.changed_paths(before, after, holder) == ("api/routes.js", "apps/web/app.ts")

    # a checkout holding another one: the nested one is read, not listed as one untracked folder
    outer = checkout(tmp_path / "outer")
    inner = checkout(outer / "vendor" / "lib")
    before = launch.tracked_state(outer)
    (inner / "fix.py").write_text("z\n")
    assert launch.changed_paths(before, launch.tracked_state(outer), outer) == ("vendor/lib/fix.py",)
    assert launch.tracked_state(tmp_path / "outer" / "vendor") is not None
    empty = tmp_path / "plain"
    empty.mkdir()
    assert launch.tracked_state(empty) is None


def test_a_cli_that_does_not_take_the_mode_runs_the_attempt_in_the_old_one(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(argv[argv.index("--permission-mode") + 1])
        if len(calls) == 1:
            return proc("", 1, "error: option '--permission-mode <mode>' argument 'auto' is invalid.")
        return proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed")))

    out = launch.run(spec(tmp_path, permission_mode="auto"), runner=runner)
    assert calls == ["auto", "acceptEdits"] and out.passed and out.permission_mode == "acceptEdits"
    # any other failure is the attempt's own, not a reason to change mode
    calls.clear()
    failing = lambda argv, **kw: calls.append(1) or proc("", 1, "network down")  # noqa: E731
    assert launch.run(spec(tmp_path, permission_mode="auto"), runner=failing).failure is Failure.ENVIRONMENT
    assert calls == [1]


def test_what_the_worker_learned_is_read_from_its_block_and_kept_short(tmp_path):
    facts = ["Prices are stored in cents", "  Prices are stored in cents ", 7, "", *(f"fact {i}" for i in range(9)),
             "x" * 2000]
    out = parse(proc(envelope(block(verdict="pass", summary="ok", evidence="1 passed", learned=facts))),
                spec(tmp_path))
    assert out.learned[0] == "Prices are stored in cents" and len(out.learned) == launch.MAX_LEARNED
    assert "fact 0" in out.learned and 7 not in out.learned
    assert launch.learned({"learned": ["y" * 2000]}) == ("y" * launch.LEARNED_CHARS,)
    assert launch.learned({"learned": "not a list"}) == () and launch.learned({}) == ()
    failed = parse(proc(envelope(block(verdict="fail", failure="code_bug", summary="no", learned=["A fact"]))),
                   spec(tmp_path))
    assert failed.learned == ("A fact",)  # read on any verdict; only a pass files it
    assert '"learned"' in launch.RESULT_INSTRUCTIONS


def test_a_refused_write_the_attempt_got_past_blocks_nothing(tmp_path):
    def refused(*paths):
        return json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "", "permission_denials": [
            {"tool_name": "Write", "tool_input": {"file_path": p}} for p in paths]})

    # written after all, per git: the attempt ran out of turns, nothing more
    got_past = parse(proc(refused(str(tmp_path / "src/routes/a.ts"))), spec(tmp_path), changed=("src/routes/a.ts",))
    assert got_past.failure is Failure.TURNS_EXHAUSTED
    assert got_past.denied == ()
    assert got_past.allow == ()
    assert got_past.overcome == ("Write(src/routes/a.ts)",)
    # one written, one not: the one not written still blocks
    half = parse(proc(refused(str(tmp_path / "src/a.ts"), str(tmp_path / "src/b.ts"))), spec(tmp_path),
                 changed=("src/a.ts",))
    assert len(half.denied) == 1
    assert half.overcome == ("Write(src/a.ts)",)
    # outside the work directory, a changed path of the same name is no evidence
    outside = parse(proc(refused("/elsewhere/src/a.ts")), spec(tmp_path), changed=("src/a.ts",))
    assert outside.denied == ("Write(/elsewhere/src/a.ts)",)
    assert outside.overcome == ()
