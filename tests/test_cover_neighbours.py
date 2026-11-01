"""The corners of the neighbours, the store, the doctor, the analysis and the UI that the main
tests leave unexercised: each test asserts what the code returns, records or writes."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cauce import acp, behavior, doctor, mcp_guard
from cauce.adapters import ABSENT, PRESENT, Status, default_adapters
from cauce.adapters import livespec as livespec_mod
from cauce.adapters.engram import Engram
from cauce.adapters.livespec import Livespec, LivespecError

from .livespec_fixture import build
from .test_acp import FakeProc as AcpFakeProc
from .test_acp import Sink as AcpSink
from .test_acp import Spawner as AcpSpawner
from .test_engram import Fake as EngramFake

# --- behavior -------------------------------------------------------------------------------


def test_a_profile_with_unreadable_stamps_measures_no_time():
    calls = [{"ts": "not a time", "tool": "Read", "sig": "read", "arg_hash": "a", "ok": 1},
             {"ts": "neither", "tool": "Edit", "sig": "edit:py", "arg_hash": "b", "ok": 1}]
    out = behavior.profile(calls)
    assert out["calls_before_edit"] == 1
    assert "s_before_edit" not in out and "gap_max_s" not in out and "active_s" not in out
    assert behavior._ts(None) is None


def test_a_profile_that_cannot_be_read_costs_the_run_nothing():
    class Broken:
        def attempt_calls(self, task_id, nth):
            raise RuntimeError("the store is gone")

    assert behavior.of_attempt(Broken(), 1, 1) == {}


# --- mcp_guard ------------------------------------------------------------------------------


def test_the_engram_guard_with_no_project_names_none(monkeypatch):
    monkeypatch.delenv("ENGRAM_PROJECT", raising=False)
    assert mcp_guard.engram("mem_search", {"query": "x"}) == (None, {"query": "x"})


def test_cauce_home_is_read_as_the_store_reads_it(tmp_path):
    assert mcp_guard.cauce_home({"CAUCE_HOME": "/x/home"}) == Path("/x/home")
    assert mcp_guard.cauce_home({"XDG_DATA_HOME": "/data", "HOME": "/h"}) == Path("/data/cauce")
    assert mcp_guard.cauce_home({"XDG_DATA_HOME": "relative", "HOME": "/h"}) == Path("/h/.local/share/cauce")


def _act_loop(home: Path, **loop) -> dict[str, str]:
    folder = home / "loops"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "grow.json").write_text(json.dumps({"mode": "act", **loop}))
    return {"CAUCE_HOME": str(home), "CAUCE_OUTBOUND_ACTS": "send", "CAUCE_LOOP": "grow"}


def test_an_acting_loop_with_no_hours_and_no_cap_logs_every_call(tmp_path):
    env = _act_loop(tmp_path)
    for _ in range(3):
        assert mcp_guard.outbound("send", {"to": "a@b"}, env, now=1000.0) == (None, {"to": "a@b"})
    log = (tmp_path / "loops" / "grow.sent.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in log] == [{"at": 1000.0, "tool": "send"}] * 3


def test_the_guard_ends_quietly_when_the_worker_stops_listening():
    class Gone(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError

    refused = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "rastro_profiles"}}).encode() + b"\n"
    code = mcp_guard.serve("rastro", [sys.executable, "-c", "import sys; sys.stdin.read()"],
                           io.BytesIO(refused), Gone())
    assert code == 0


def test_the_guard_says_how_it_is_run(capsys):
    assert mcp_guard.main(["rastro"]) == 2
    assert mcp_guard.main(["nope", "--", "x"]) == 2
    assert mcp_guard.main(["rastro", "x", "y"]) == 2
    assert "usage: python -m cauce.mcp_guard {rastro,engram,livespec,outbound}" in capsys.readouterr().err


def test_the_guard_s_command_line_serves_stdio(monkeypatch):
    class Std:
        def __init__(self, data=b""):
            self.buffer = io.BytesIO(data)

    out = Std()
    monkeypatch.setattr(sys, "stdin", Std(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'))
    monkeypatch.setattr(sys, "stdout", out)
    echo = "import sys; [print(line.strip(), flush=True) for line in sys.stdin]"
    assert mcp_guard.main(["livespec", "--", sys.executable, "-c", echo]) == 0
    assert json.loads(out.buffer.getvalue()) == {"jsonrpc": "2.0", "id": 1, "method": "ping"}


# --- adapters -------------------------------------------------------------------------------


def test_a_status_line_says_only_what_it_knows():
    assert Status("livespec", PRESENT, version="0.32.0").line() == "livespec: present v0.32.0"
    assert Status("x", ABSENT, "not indexed", stale=True).line() == "x: absent stale — not indexed"


def test_the_default_adapters_follow_their_settings(monkeypatch):
    names = [a.name for a in default_adapters({"CAUCE_LIVESPEC": "on", "CAUCE_ENGRAM": "on"})]
    assert names == ["livespec", "engram"]
    assert default_adapters({"CAUCE_LIVESPEC": "off", "CAUCE_ENGRAM": "off"}) == []


def test_a_memory_engram_saved_without_an_id_is_not_counted(tmp_path):
    fake = EngramFake(**{"POST /observations": (200, {"status": "saved"})})
    adapter = Engram({}, http=fake, which=lambda name: "/usr/bin/engram")
    ids = adapter.remember(["Prices are kept in cents."], project="shop", task_id=3, title="t", directory=tmp_path)
    assert ids == [] and len(fake.posted("/observations")) == 1
    assert fake.posted("/sessions/cauce-task-1/end")  # the session engram named is the one ended


# --- livespec -------------------------------------------------------------------------------


def _livespec(tmp_path: Path, path: str = "", **kw) -> Livespec:
    return Livespec(env={"HOME": str(tmp_path / "home"), "PATH": path, "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
                         "CAUCE_HOME": str(tmp_path / "cauce")}, **kw)


def _bin(tmp_path: Path, name: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / name).write_text("#!/bin/sh\n")
    (bindir / name).chmod(0o755)
    return str(bindir)


def _plugin(tmp_path: Path, version: str, text: str) -> Path:
    cfg = tmp_path / "cc" / "plugins" / "cache" / "m" / "livespec" / version / ".mcp.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    return cfg


def _garbage_status(tmp_path: Path) -> Status:
    db = tmp_path / "garbage.db"
    db.write_bytes(b"this is not a database, not even close to one")
    return Status("livespec", PRESENT, "x", data={"db": str(db), "project": 1})


def test_an_index_run_that_fails_raises_when_asked_to_check():
    with pytest.raises(subprocess.CalledProcessError):
        livespec_mod._run_in_group(["sh", "-c", "exit 3"], timeout=10, env=dict(os.environ), check=True)


def test_a_uvx_entry_that_names_no_release_names_none():
    assert livespec_mod._uvx_release({"command": "uvx", "args": ["--from", "somewhere", "livespec"]}) is None


def test_a_broken_or_commandless_plugin_entry_gives_way_to_an_older_one(tmp_path):
    _plugin(tmp_path, "0.40.0", "not json at all")
    _plugin(tmp_path, "0.39.0", json.dumps({"mcpServers": {"livespec": {"args": []}}}))
    _plugin(tmp_path, "0.38.0", json.dumps({"mcpServers": {"livespec": {"command": "uvx",
                                                                        "args": ["livespec@0.38.0"]}}}))
    assert _livespec(tmp_path).server() == {"command": "uvx", "args": ["livespec@0.38.0"]}


def test_the_version_of_a_plugin_s_own_uv_running_the_pinned_release_is_the_pin(tmp_path):
    _plugin(tmp_path, "0.40.0", json.dumps({"mcpServers": {"livespec": {"command": "/opt/uv",
                                                                        "args": ["tool", "run", "livespec@0.40.0"]}}}))
    a = _livespec(tmp_path)
    a._remember_unpublished("0.40.0")
    assert a.server() == {"command": "/opt/uv", "args": ["tool", "run", f"livespec@{livespec_mod.PINNED}"]}
    assert a._version() == livespec_mod.PINNED


def test_a_folder_that_cannot_be_listed_holds_no_repository(tmp_path):
    assert _livespec(tmp_path).roots(tmp_path / "missing") == []


def test_an_old_livespec_is_said_to_predate_what_the_adapter_was_checked_against(tmp_path, git_repo):
    build(git_repo)
    _plugin(tmp_path, "0.1.0", json.dumps({"mcpServers": {"livespec": {"command": "livespec", "args": []}}}))
    status = _livespec(tmp_path).inspect(git_repo)
    assert status.state == PRESENT and "v0.1.0 predates the 0.32.0" in status.detail


def test_a_failure_never_recorded_was_never_said(tmp_path, git_repo):
    assert _livespec(tmp_path).failure_said(git_repo) is False


def test_a_refresh_that_waited_runs_when_the_index_is_still_behind(tmp_path, git_repo, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def waited(path, *, wait_s, **kw):
        yield True, True

    monkeypatch.setattr(livespec_mod, "_lock", waited)
    ran = []
    a = _livespec(tmp_path, _bin(tmp_path, "uvx"),
                  runner=lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert a.refresh(git_repo) == "livespec index refreshed" and len(ran) == 1


def test_a_release_uv_cannot_find_with_no_other_way_to_run_is_a_failure(tmp_path, git_repo):
    _plugin(tmp_path, "0.40.0", json.dumps({"mcpServers": {"livespec": {"command": "uvx",
                                                                        "args": ["--from", "x", "livespec"]}}}))
    said = ("No solution found when resolving tool dependencies: because livespec==0.40.0 was not found in the "
            "package registry")
    a = _livespec(tmp_path, runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", said))
    out = a.refresh(git_repo)
    assert out.startswith("livespec index failed: No solution found") and "pinned" not in out
    assert "0.40.0" in a.unpublished()


def test_a_fallback_to_the_pinned_release_that_cannot_run_says_both(tmp_path, git_repo):
    _plugin(tmp_path, "0.40.0", json.dumps({"mcpServers": {"livespec": {"command": "uvx",
                                                                        "args": ["livespec@0.40.0"]}}}))
    calls = []

    def runner(argv, **kw):
        calls.append(argv[1])
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "livespec==0.40.0 was not found in the package registry")
        raise OSError("uvx vanished")

    out = _livespec(tmp_path, _bin(tmp_path, "uvx"), runner=runner).refresh(git_repo)
    assert out == (f"livespec 0.40.0 is not published, so the pinned {livespec_mod.PINNED} ran instead (and will, "
                   "until the plugin names a published release); livespec index did not run: uvx vanished")


def test_a_repository_whose_refresh_will_not_start_settles_as_failed(tmp_path, git_repo):
    def broken(*a, **kw):
        raise OSError("no processes left")

    a = _livespec(tmp_path, _bin(tmp_path, "uvx"), popen=broken)
    wait = livespec_mod._Wait(0.0, 100.0, 100.0, lambda s: None, lambda: 1.0)
    found = a._settled(git_repo.resolve(), None, "this task runs without it", wait)
    assert found.outcome == livespec_mod.FAILED
    assert found.detail == "indexing failed (it left no usable index); this task runs without it"


def test_a_server_that_breaks_mid_conversation_did_not_answer(tmp_path):
    class Proc:
        class stdin:
            @staticmethod
            def write(text):
                raise OSError("pipe closed")

        def kill(self):
            pass

        def wait(self):
            return 0

    a = _livespec(tmp_path, _bin(tmp_path, "livespec"), popen=lambda *a, **kw: Proc())
    with pytest.raises(LivespecError, match="livespec did not answer: pipe closed"):
        a.call_tools(tmp_path, [("search", {})], timeout_s=5)


def _specs(git_repo: Path) -> None:
    (git_repo / "openspec" / "specs").mkdir(parents=True)
    (git_repo / "openspec" / "specs" / "a.md").write_text("# a\n")


def test_validate_openspec_without_livespec_or_a_copy_or_an_index_says_why(tmp_path, git_repo, monkeypatch):
    _specs(git_repo)
    with pytest.raises(LivespecError, match="no livespec to ask"):
        _livespec(tmp_path).validate_openspec(git_repo)

    def raising(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(LivespecError, match="livespec index did not run"):
        _livespec(tmp_path, _bin(tmp_path, "livespec"), runner=raising).validate_openspec(git_repo)

    def no_copy(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(livespec_mod.shutil, "copytree", no_copy)
    with pytest.raises(LivespecError, match="openspec/ could not be copied: disk full"):
        _livespec(tmp_path, _bin(tmp_path, "livespec")).validate_openspec(git_repo)


def test_an_unreadable_index_tells_nothing_and_says_so_where_it_must(tmp_path, git_repo):
    status = _garbage_status(tmp_path)
    a = _livespec(tmp_path)
    assert a.brief("charge_card fails", git_repo, status).reasons[0].startswith(
        "livespec index unreadable while briefing:")
    assert a.symbols_named("charge_card fails", status) == []
    assert a.reach(["src/billing.py"], git_repo, status) == ()
    assert a.assess(["src/billing.py"], git_repo, status)[0].startswith("livespec: could not assess the change (")
    assert a.unlinked_specs(status) == []


def test_a_map_of_quiet_code_raises_nothing_and_reach_names_the_tests(tmp_path, git_repo):
    build(git_repo)
    a = _livespec(tmp_path)
    status = a.inspect(git_repo)
    quiet = a.brief("src.api.checkout", git_repo, status)
    assert quiet.lines and "`src.api.checkout`" in quiet.lines[1]
    assert quiet.raise_rungs == 0 and not quiet.critical and quiet.reasons == ()
    reach = a.reach(["src/parse.py"], git_repo, status)
    assert "livespec: tests that exercise them: tests/test_parse.py" in reach
    conn = livespec_mod._connect(Path(status.data["db"]))
    assert livespec_mod._matching_symbols(conn, 1, "") == []
    conn.close()


def test_the_mcp_client_skips_other_answers_and_raises_on_an_error():
    class Proc:
        def __init__(self, lines):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))

    found = livespec_mod._ask(Proc([{"id": 9, "result": "other"}, {"id": 1, "result": {"ok": 1}}]), 1, "x", {})
    assert found == {"ok": 1}
    with pytest.raises(LivespecError, match="tools/call: denied"):
        livespec_mod._ask(Proc([{"id": 2, "error": {"message": "denied"}}]), 2, "tools/call", {})
    with pytest.raises(LivespecError, match="validate_openspec: no answer"):
        livespec_mod._tool_data("validate_openspec", None)


def test_git_questions_about_a_folder_that_is_gone_tell_nothing(tmp_path):
    assert livespec_mod._head(tmp_path / "gone") is None
    assert livespec_mod._older_than_head(None, tmp_path) is True
    assert livespec_mod._older_than_head("not a date", tmp_path) is False


# --- acp ------------------------------------------------------------------------------------


def _agent(**kw) -> tuple[acp.Agent, AcpSink, io.StringIO]:
    out, err = AcpSink(), io.StringIO()
    args = {"popen": AcpSpawner(), "killpg": lambda pid, sig: None, "grace": 1.0,
            "diffs": lambda task, live: [], "look": lambda task, ended: acp.Look(), **kw}
    return acp.Agent(out, err, args.pop("popen"), args.pop("killpg"), args.pop("grace"), **args), out, err


def _run(**kw) -> acp.Run:
    return acp.Run(proc=AcpFakeProc(["cauce"], {}, **kw.pop("proc", {})), call_id="cauce-1", request_id=7, **kw)


def test_a_prompt_s_text_skips_what_is_no_block():
    assert acp.prompt_text(["loose text", 3, {"type": "text", "text": "fix it"}]) == "fix it"


def test_the_diffs_of_a_task_with_nothing_to_show_are_none(store):
    assert acp.task_diffs(999, True) == []
    task = store.create_task("x", status="done", source="queue", repo="r", cwd="/work/app")
    store.add_event(task["id"], "finished", status="done")
    assert acp.task_diffs(task["id"], False) == []


def test_a_verdict_says_what_was_not_self_checked_and_nothing_of_a_check_it_cannot_tell():
    assert acp.verdict_text("not a card") == []
    lines = acp.verdict_text({"check": None, "independent": {"ran": False}, "unchecked": "it ran out of turns"})
    assert lines == ["check: no check was given, so nothing ran the tests", "not self-checked: it ran out of turns"]
    based = acp.verdict_text({"check": {"command": "npm test", "state": "failing at base, unchanged"}})
    assert based == ["check: cauce ran `npm test` and it passed (failing at base, unchanged: it fails at the base "
                     "commit too, so the work stood)"]


def test_a_stop_with_no_command_to_go_on_says_what_to_do_alone():
    text = acp.ending_text(5, {"status": "blocked", "stop": {"cause": "converged", "by": "cauce",
                                                             "reason": "two cells gave the same findings"}})
    assert text.splitlines()[1].startswith("Next: ")
    assert "The command that goes on" not in text


def test_the_wire_goes_quiet_once_the_client_is_gone():
    class Broken(AcpSink):
        def write(self, text):
            raise BrokenPipeError

    agent = acp.Agent(Broken(), io.StringIO(), AcpSpawner(), lambda pid, sig: None, 1.0)
    agent.reply(1, {})
    assert agent._gone is True
    agent.reply(2, {})  # said to nobody, and no error


def test_messages_without_a_method_params_or_a_handler_that_works_are_answered_as_json_rpc_says():
    agent, out, err = _agent()
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 3}))
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 4, "result": {}}))  # a client's answer: nothing
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "authenticate"}))  # no params at all
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 6, "method": "authenticate", "params": [1]}))
    agent._handle(json.dumps({"jsonrpc": "2.0", "method": "session/prompt", "params": {}}))  # a notification

    def broken(params, request_id):
        raise KeyError("boom")

    agent._methods["initialize"] = broken
    agent._handle(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}}))
    agent._handle(json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}}))
    assert out.messages() == [
        {"jsonrpc": "2.0", "id": 3, "error": {"code": -32600, "message": "Invalid Request"}},
        {"jsonrpc": "2.0", "id": 5, "result": {}},
        {"jsonrpc": "2.0", "id": 6, "error": {"code": -32602, "message": "params must be an object"}},
        {"jsonrpc": "2.0", "id": 8, "error": {"code": -32603, "message": "Internal error"}}]
    assert err.getvalue().count("KeyError: 'boom'") == 2


def test_a_run_whose_prompt_was_already_answered_ends_without_a_second_answer():
    agent, out, _ = _agent()
    session = acp.Session(cwd="/w", mcp_ignored=False)
    run = _run(proc={"stdout": "the report\n"}, answered=True)
    agent._finish("s1", session, run)
    kinds = [m["params"]["update"]["sessionUpdate"] for m in out.messages() if m.get("method")]
    assert kinds == ["tool_call_update", "agent_message_chunk"]
    assert not [m for m in out.messages() if m.get("id") == 7]


def test_a_kept_branch_that_cannot_be_read_costs_the_view_never_the_end():
    def diffs(task, live):
        raise OSError("no such worktree")

    agent, out, err = _agent(diffs=diffs)
    run = _run(task=7)
    agent._show_end("s1", run, "completed")
    assert run.diffs == [] and "OSError: no such worktree" in err.getvalue()
    assert out.messages()[-1]["params"]["update"]["status"] == "completed"


def test_stderr_blank_lines_and_lines_after_the_progress_line_leave_the_title():
    agent, out, _ = _agent()
    run = _run(proc={"stderr": ["", "   ", "cauce: #7 attempt 1"]}, look=acp.Look(line="implementing · 1m"))
    run.title = "implementing · 1m"
    agent._follow_stderr("s1", run)
    assert run.task == 7 and list(run.tail) == ["cauce: #7 attempt 1"]
    assert run.title == "implementing · 1m"
    shown = [m["params"]["update"] for m in out.messages()]
    assert all(u.get("title") != "cauce: #7 attempt 1" for u in shown)


def test_a_plan_read_after_the_run_closed_is_not_said():
    agent, out, _ = _agent()
    run = _run(task=7, planned=True, closed=True)
    agent._plan("s1", run)
    assert out.messages() == []
    agent._plan("s1", run, last=True)
    assert out.messages()[0]["params"]["update"]["sessionUpdate"] == "plan"


def test_reading_a_run_with_no_task_or_diffs_that_fail_leaves_it_as_it_was():
    def diffs(task, live):
        raise RuntimeError("git is gone")

    agent, _, err = _agent(diffs=diffs, look=lambda task, ended: pytest.fail("no task, no look"))
    run = _run()
    agent._read_look(run)
    agent._read_diffs(run)
    assert run.look == acp.Look() and run.diffs == []
    run.task, run.diffs = 7, [{"type": "diff"}]
    agent._read_diffs(run)
    assert run.diffs == [{"type": "diff"}] and "RuntimeError: git is gone" in err.getvalue()


def test_following_a_task_reads_each_view_when_due_and_stops_saying_once_the_run_closed():
    class Ticks:
        """`exited.wait` that lets the loop go round `n` times."""

        def __init__(self, n):
            self.n = n

        def wait(self, timeout=None):
            self.n -= 1
            return self.n < 0

    looks, reads = [], []

    def diffs(task, live):
        reads.append(live)
        if len(reads) == 2:
            run.closed = True
        return []

    agent, out, _ = _agent(diffs=diffs, look=lambda task, ended: looks.append(ended) or acp.Look(line="busy"),
                           look_every=1000.0, diff_every=0.0)
    run = _run(task=7, planned=True)
    run.exited = Ticks(3)
    agent._follow_task("s1", run)
    assert looks == [False] and reads == [True, True]  # the look once (not due again), the diffs each time
    plans = [m for m in out.messages() if m["params"]["update"]["sessionUpdate"] == "plan"]
    assert len(plans) == 1  # said after the first round only: the second closed the run


def test_a_cancel_cauce_cannot_start_is_said_on_stderr():
    def popen(*a, **kw):
        raise OSError("fork failed")

    agent, _, err = _agent(popen=popen)
    agent._cancel_task("/w", 7)
    assert "cauce acp: `cauce cancel 7` did not finish: fork failed" in err.getvalue()


# --- doctor ---------------------------------------------------------------------------------

OLD = "0.1.0"  # a release before every rule's fix


def _rule(rule, store, **kw) -> list[tuple[str, int | None]]:
    from datetime import UTC, datetime

    return [(f.severity, f.task_id) for f in rule(store, since=None, now=kw.pop("now", datetime.now(UTC)), **kw)]


def test_the_doctor_reads_unreadable_stamps_and_options_as_nothing():
    assert doctor._ts("not a time") is None
    assert doctor._options({"options": "{broken"}) == {}
    assert doctor._options({"options": "[1]"}) == {}


def test_a_wait_judged_on_nothing_or_recently_is_no_finding(store):
    from datetime import UTC, datetime, timedelta

    store.create_task("long", status="running", source="queue", repo="r", pid=1)
    held = store.create_task("add a flag", status="queued", source="queue", repo="r")
    store.set_parallel(held["id"], False, "x")
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="x", ahead=[])
    later = datetime.now(UTC) + timedelta(hours=1)
    assert _rule(doctor.wait_judged_on_gone, store, now=later, alive=lambda pid: True) == []


def test_a_part_that_lost_its_mark_before_the_fix_is_history(store):
    part = {"id": "api", "n": 1, "of": 2, "plan": 1}
    store.create_task("a", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                      options=json.dumps({"part": part}))
    lost = store.create_task("b", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build")
    store.add_event(lost["id"], "finished", status="done", version=OLD)
    assert _rule(doctor.parts_that_lost_their_mark, store) == [(doctor.FIXED, lost["id"])]


def test_a_join_short_of_parts_before_the_fix_is_history_and_a_part_with_no_branch_is_not_counted(store):
    part = {"id": "api", "n": 1, "of": 3, "plan": 1}
    for n, branch in enumerate(("cauce/task-a", "cauce/task-b", None)):
        t = store.create_task(f"p{n}", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                              options=json.dumps({"part": {**part, "n": n + 1}}))
        store.add_event(t["id"], "finished", status="done", branch=branch)
    join = store.create_task("join", status="done", source="queue", repo="r", workflow_run=7,
                             workflow_step="integrate")
    store.add_event(join["id"], "finished", status="done", skipped=True, version=OLD)
    found = list(doctor.joins_short_of_their_parts(store, since=None))
    assert [(f.severity, f.task_id) for f in found] == [(doctor.FIXED, join["id"])]
    assert found[0].what == f"1 record(s) from before 0.29.2, the release that fixes it (latest #{join['id']})"


def test_old_worktree_attempts_that_kept_their_session_are_not_history(store):
    t = store.create_task("t", status="done", source="cauce", repo="r", kind="implement")
    store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=80, version=OLD)
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=80, passed=1, session_id="kept")
    assert _rule(doctor.attempts_with_no_session, store) == []


def test_a_format_climb_before_the_fix_is_history_and_a_climb_on_real_work_is_none(store):
    for summary, version in (("its plan cannot run in parts: no prompt", OLD), ("the totals are still wrong", OLD)):
        t = store.create_task("plan", status="done", source="cauce", repo="r", kind="plan")
        store.add_attempt(t["id"], cell="opus/xhigh", max_turns=80, passed=0, failure="inconclusive", summary=summary)
        store.add_event(t["id"], "moved", seq=1, axis="effort", trigger="inconclusive", version=version)
    assert _rule(doctor.format_failures_that_climbed, store) == [(doctor.FIXED, 1)]


def test_fix_rounds_with_nothing_before_them_on_their_base_or_before_the_fix(store):
    run = store.create_run(name="feature", scope="bundled", source="x", repo="r",
                           definition=json.dumps({"name": "feature", "steps": []}))

    def fix(round_, base, status="done", version=None):
        t = store.create_task("fix", status=status, source="queue", repo="r", workflow_run=run["id"],
                              workflow_step="fix", options=json.dumps({"ledger": {"role": "fix", "round": round_,
                                                                                  "base": base}}))
        store.add_event(t["id"], "planned", version=version)
        return t

    alone = fix(2, "b0")  # nothing done before it in its step
    assert _rule(doctor.fix_rounds_off_their_base, store) == []
    first = fix(1, "b0")
    store.add_event(first["id"], "finished", status="done", branch="cauce/x", tip="t1")
    fix(2, "t1", status="running")  # on what round 1 kept
    old = fix(2, "b0", status="running", version=OLD)
    assert alone["id"] < first["id"]
    assert _rule(doctor.fix_rounds_off_their_base, store) == [(doctor.FIXED, old["id"])]


def test_a_revision_with_one_verdict_of_a_kind_is_judged_once(store):
    from .test_revisions import _candidate

    first = _candidate(store)["id"]
    store.add_role_revision(at="2026-10-02T00:00:00+00:00", repo="r", role="implementer", record="adopted",
                            revision=first, hash="h", version="0.32.0")
    assert _rule(doctor.revisions_judged_twice, store) == []


def test_a_trial_never_sampled_before_the_fix_is_history(store):
    from datetime import UTC, datetime, timedelta

    from .test_revisions import _task

    opened = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    cand = store.add_role_revision(at=opened, repo="r", role="implementer", record="proposed", hash="h",
                                   lessons=["x"], version="0.31.0")
    _task(store)
    found = list(doctor.trials_never_sampled(store, now=datetime.now(UTC) + timedelta(days=4)))
    assert [(f.severity, f.task_id) for f in found] == [(doctor.FIXED, cand)]


def test_a_failed_fix_recorded_after_its_task_passed_opens_no_finding(store):
    task = store.create_task("fix the ble race", status="done", source="cauce", repo="r")
    store.add_event(task["id"], "finished", status="done", version="0.40.0")
    store._conn.execute("UPDATE events SET ts = '2000-01-01T00:00:00+00:00' WHERE task_id = ?", (task["id"],))
    problem = store.open_problem("fix the ble race", repo="r")
    store.add_fix(problem, "sonnet/high: retried", "failed", repo="r", task_id=task["id"])
    assert _rule(doctor.problems_open_after_pass, store) == []


def test_flake_fixes_and_short_turns_before_their_fix_are_history(store):
    flake = store.create_task("fix the flaky ble test", status="done", source="cauce", repo="r", kind="bugfix")
    store.add_event(flake["id"], "finished", status="done", version=OLD,
                    verdict={"check": {"command": "deno task test", "passed": True}})
    assert _rule(doctor.flake_fixes_checked_once, store) == [(doctor.FIXED, flake["id"])]
    long = store.create_task("ui", status="done", source="cauce", repo="r")
    store.add_event(long["id"], "attempt_finished", seq=1, wall_s=48 * 60, turns=2, version=OLD)
    assert _rule(doctor.turns_under_the_wall, store) == [(doctor.FIXED, long["id"])]


def test_a_pickup_with_an_unreadable_stamp_is_skipped(store):
    t = store.create_task("x", status="done", source="cauce", repo="r")
    event = store.add_event(t["id"], "attempt_started", seq=1)
    store._conn.execute("UPDATE events SET ts = 'garbage' WHERE id = ?", (event,))
    assert _rule(doctor.pickups_nobody_saw, store) == []


def test_runs_with_no_folder_name_no_checkout_to_read(store, tmp_path):
    t = store.create_task("x", status="done", source="cauce", repo="r")
    store.add_event(t["id"], "planned")
    assert doctor._checkouts(store, None, _livespec(tmp_path)) == []


# --- analysis -------------------------------------------------------------------------------


def _event(n, name, ts, **data):
    return {"id": n, "task_id": 1, "kind": name, "ts": ts, "data": data}


def test_a_run_reads_past_events_it_does_not_count_and_a_death_it_cannot_time():
    from cauce import analysis

    task = {"id": 1, "repo": "r", "title": "t", "kind": "implement", "source": "queue"}
    found = analysis.runs([task], [
        _event(1, "planned", "2026-10-01T10:00:00+00:00", kind="implement"),
        _event(2, "stage", "2026-10-01T10:00:01+00:00", stage="worker"),
        _event(3, "attempt_started", "2026-10-01T10:00:02+00:00", max_turns=40),
        _event(4, "finished", "not a time", status="interrupted")])
    assert len(found) == 1 and found[0]["status"] == "interrupted"
    assert found[0]["lost_s"] is None and found[0]["unnoticed_s"] is None


def test_compactions_read_what_followed_each_decision_and_skip_what_belongs_to_none():
    from cauce import analysis, compaction

    rows = analysis.compactions([
        {"id": 1, "kind": compaction.DECIDED, "ts": "t", "data": {"tokens": 100, "window": 200}},
        {"id": 2, "kind": compaction.DELIVERED, "ts": "t", "data": {"decision": 99, "how": "mod"}},
        {"id": 3, "kind": compaction.SETTLED, "ts": "t", "data": {"decision": 1, "ran": True, "evidence": "hook"}},
        {"id": 4, "kind": compaction.CONTINUED, "ts": "t", "data": {"decision": 1}},
        {"id": 5, "kind": compaction.SEEN, "ts": "t", "data": {"marker": 1, "source": "transcript"}},
        {"id": 6, "kind": "something else", "ts": "t", "data": {"decision": 1}}])
    assert len(rows) == 1
    assert (rows[0]["ran"], rows[0]["evidence"], rows[0]["continued"], rows[0]["delivered"]) == (
        True, "hook", True, None)
    assert rows[0]["share"] == 0.5


def test_an_expensive_run_that_raced_says_so():
    from cauce import analysis

    found = [{"task": 1, "title": "t", "kind": "implement", "cost_usd": 9.0, "moves": ["race"], "cells": [],
              "turns": 0, "verifier_cost_usd": 0.0, "check_s": None, "wall_s": None, "refused": 0, "stop": None,
              "attempts": 1}]
    assert analysis.expensive(found)[0]["why"] == ["raced a second worker"]


def test_the_stats_text_says_where_each_trial_stands_now(git_repo, store):
    from cauce import analysis
    from cauce.orchestrate import run as run_task

    from .test_orchestrate import Script, kind, ok

    run_task("fix the cart total", git_repo, store, registry={}, launcher=Script(ok(cost=0.2)),
             classifier=kind("implement"))
    report = analysis.compute(store, days=0)
    arm = {"tasks": 2, "passed_first": 1, "calls_before_edit_p50": None, "input_tokens_p50": None,
           "duration_p50_s": None}
    side = {"tasks": 2, "passed": 1, "tries": 2, "rate": 0.5}
    report.update(primer={"primer": arm}, primer_now="kept: fewer calls", trunk={"fork": arm},
                  trunk_now="forking pays", recall={"fused": side}, recall_now="fusion kept",
                  evidence={"gate": {"tasks": 2, "read": 2, "blocked": 1, "blocked_rate": 0.5}},
                  evidence_now="gate kept", methods={"diagnosis": {"with": side}, "premortem": {"with": {}}},
                  methods_now={})
    shown = analysis.text(report)
    for line in ("  now: kept: fewer calls", "  now: forking pays", "the order of a brief's notes",
                 "  fused    tasks    2  first tries 1/2 (50%)", "  now: fusion kept",
                 "  gate   tasks    2  read    2  blocked    1 (50%)", "  now: gate kept",
                 "the diagnosis method (its trial; tasks, first tries passed):"):
        assert line in shown, line
    assert "the premortem method" not in shown
    report.update(recall_now=None, evidence_now=None)
    quiet = analysis.text(report)
    assert "the order of a brief's notes" in quiet and "now: fusion kept" not in quiet and "now: gate kept" not in quiet
