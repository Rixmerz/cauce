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
