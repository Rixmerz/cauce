from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from cauce.adapters import ABSENT, PRESENT, UNREADABLE, Status
from cauce.adapters import livespec as livespec_mod
from cauce.adapters.livespec import _HINTS, PINNED, TOOLS, Livespec, is_test_path
from cauce.adapters.livespec import _lock as lock

from .livespec_fixture import build


def adapter(tmp_path: Path, path: str = "", **kw) -> Livespec:
    env = {"HOME": str(tmp_path / "home"), "PATH": path, "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
           "CAUCE_HOME": str(tmp_path / "cauce")}
    return Livespec(env=env, **kw)


def tool(tmp_path: Path, name: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return str(bindir)


def test_how_livespec_runs_plugin_then_path_then_pinned(tmp_path):
    assert adapter(tmp_path).server() is None
    assert adapter(tmp_path, tool(tmp_path, "uvx")).server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}
    assert adapter(tmp_path, tool(tmp_path, "livespec")).server() == {"command": "livespec", "args": []}
    for version in ("0.31.0", "0.32.1"):
        cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / version / ".mcp.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": [f"livespec@{version}"]}}}))
    a = adapter(tmp_path)
    assert a.server()["args"] == ["livespec@0.32.1"] and a._version() == "0.32.1"


def test_absent_present_stale_and_unreadable(tmp_path, git_repo):
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    status = a.inspect(git_repo)
    assert status.state == ABSENT and "not indexed" in status.detail
    assert adapter(tmp_path).inspect(git_repo).detail.startswith("cannot run")

    build(git_repo)
    status = a.inspect(git_repo / "src" if (git_repo / "src").exists() else git_repo)
    assert status.state == PRESENT and not status.stale and status.data["project"] == 1

    build_stale = git_repo.parent / "stale"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(build_stale)], check=True)
    build(build_stale, indexed_at="2000-01-01 00:00:00")
    assert a.inspect(build_stale).stale

    other = git_repo.parent / "other"
    subprocess.run(["git", "clone", "-q", str(git_repo), str(other)], check=True)
    build(other, project_root=Path("/somewhere/else"))
    assert a.inspect(other).state == ABSENT

    (git_repo / ".mcp-docs" / "docs.db").write_bytes(b"not a database at all, not even close")
    assert a.inspect(git_repo).state == UNREADABLE


def test_group_db_is_honoured(tmp_path, git_repo):
    shared = tmp_path / "shared"
    db = build(shared, project_root=git_repo)
    (git_repo / ".livespec.toml").write_text(f'[workspace]\ngroup_db = "{db}"\n')
    assert Livespec.db_path(git_repo) == db
    assert adapter(tmp_path).inspect(git_repo).state == PRESENT
    (git_repo / ".livespec.toml").write_text("[workspace]\ngroup_db = \"../shared/.mcp-docs/docs.db\"\n")
    assert Livespec.db_path(git_repo) == db.resolve()
    (git_repo / ".livespec.toml").write_text("not = [toml")
    assert Livespec.db_path(git_repo) == git_repo / ".mcp-docs" / "docs.db"


def test_the_briefing_maps_the_code_and_raises_the_start(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    briefing = a.brief("charge_card double-charges when the amount has cents", git_repo, status)
    text = "\n".join(briefing.lines)
    assert "`src.billing.charge_card`" in text and "SPEC-7 Card charges are idempotent [critical]" in text
    assert "tests.test_parse" not in text  # tests reach the map through what they call
    assert briefing.critical and briefing.raise_rungs == 1
    widely = a.brief("parse_amount rounds wrong", git_repo, status)
    assert "22 caller(s)" in "\n".join(widely.lines) and "tests/test_parse.py" in "\n".join(widely.lines)
    assert widely.raise_rungs == 1 and not widely.critical
    assert a.brief("nothing here matches zzzz", git_repo, status).lines == ()
    assert a.brief("x", git_repo, a.inspect(tmp_path)).lines == ()


def test_the_assessment_names_specs_and_untouched_callers(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    lines = a.assess(["src/billing.py"], git_repo, status)
    assert "SPEC-7" in lines[0] and "src/api.py" in lines[1]
    tested = a.assess(["src/parse.py"], git_repo, status)
    assert any("tests that exercise" in line and "tests/test_parse.py" in line for line in tested)
    assert a.assess(["README.md"], git_repo, status) == ()
    assert a.assess([], git_repo, status) == ()


def test_refresh_uses_livespecs_own_cli(tmp_path, git_repo):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    assert a.refresh(git_repo) == "livespec index refreshed"
    assert calls[0] == ["uvx", f"livespec@{PINNED}", "index", str(git_repo.resolve())]
    failing = adapter(tmp_path, tool(tmp_path, "uvx"),
                      runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    assert "failed" in failing.refresh(git_repo)

    def raising(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert "did not run" in adapter(tmp_path, tool(tmp_path, "uvx"), runner=raising).refresh(git_repo)
    assert "cannot run" in adapter(tmp_path).refresh(git_repo)


def test_a_plugin_naming_an_unpublished_release_falls_back_to_the_pinned_one(tmp_path, git_repo):
    cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / "0.33.0" / ".mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.33.0"]}}}))
    calls = []

    def runner(argv, **kw):
        calls.append(argv[1])
        if argv[1] == "livespec@0.33.0":
            return subprocess.CompletedProcess(argv, 1, "", "  × No solution found when resolving tool "
                                               "dependencies:\n  ╰─▶ Because livespec==0.33.0 was not found in "
                                               "the package registry, we can conclude ...")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    assert a.server()["args"] == ["livespec@0.33.0"]
    said = a.refresh(git_repo)
    assert calls == ["livespec@0.33.0", f"livespec@{PINNED}"]
    assert "livespec 0.33.0 is not published, so the pinned" in said and said.startswith("livespec index refreshed")
    # Remembered: workers' servers and the next index use the pinned release at once.
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]} and a._version() == PINNED
    calls.clear()
    assert a.refresh(git_repo) == "livespec index refreshed" and calls == [f"livespec@{PINNED}"]
    # Without uvx left to run the pinned one, the plugin's entry is all there is.
    assert adapter(tmp_path).server()["args"] == ["livespec@0.33.0"]
    # A failure that is not a missing release is no fallback.
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.34.0"]}}}))
    other = adapter(tmp_path, tool(tmp_path, "uvx"),
                    runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 2, "", "index crashed"))
    assert other.refresh(git_repo) == "livespec index failed: index crashed"
    assert "0.34.0" not in other.unpublished()
    assert livespec_mod._uvx_release({"command": "livespec"}) is None
    assert livespec_mod._uvx_release({"command": "uvx", "args": ["--from", "x", "livespec==1.2"]}) == "1.2"


def test_two_refreshes_of_one_index_never_run_at_once(tmp_path, git_repo, monkeypatch):
    """Parallel tasks in one repository each plan, and each would refresh a stale
    index: livespec then fails with "database is locked" in all but one."""
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    a = adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)
    with lock(a.lock_path(git_repo), wait_s=0):
        assert "still running" in a.refresh(git_repo, wait_s=0)
    assert calls == []
    # One that waited, and finds the index current, does not index again.
    fresh = Status("livespec", PRESENT, "indexed now", PINNED, stale=False)
    monkeypatch.setattr(a, "inspect", lambda repo_dir: fresh)
    waits = iter([(True, True)])
    monkeypatch.setattr(livespec_mod, "_lock", lambda path, wait_s: _fixed(next(waits)))
    assert "the run this one waited for" in a.refresh(git_repo)
    assert calls == []
    # The lock is per index file: a repository whose index is another file has its own.
    other = git_repo.parent / "other"
    other.mkdir()
    assert a.lock_path(other) != a.lock_path(git_repo)


@contextlib.contextmanager
def _fixed(value):
    yield value


def test_the_lock_waits_then_gives_up(tmp_path):
    path = tmp_path / "x.lock"
    ticks = iter([0.0, 0.5, 2.0])
    with lock(path, wait_s=0), lock(path, wait_s=1.0, pause=lambda s: None, clock=lambda: next(ticks)) as got:
        assert got == (False, True)


def test_background_refresh_never_waits(tmp_path, git_repo):
    started = []
    a = adapter(tmp_path, tool(tmp_path, "uvx"), popen=lambda argv, **kw: started.append((argv, kw)))
    assert a.refresh_in_background(git_repo, tmp_path / "logs" / "livespec.log")
    argv, kw = started[0]
    # Through cauce, so the refresh holds the same lock as every other one.
    assert argv[1:] == ["-m", "cauce", "index-livespec", str(git_repo.resolve())] and kw["start_new_session"]
    assert kw["env"]["CAUCE_HOOKS_OFF"] == "1"
    with lock(a.lock_path(git_repo), wait_s=0):
        assert a.indexing(git_repo) and not a.refresh_in_background(git_repo, tmp_path / "logs" / "livespec.log")
    assert len(started) == 1 and not a.indexing(git_repo)
    assert not adapter(tmp_path).refresh_in_background(git_repo, tmp_path / "x.log")

    def broken(*a, **k):
        raise OSError("no")

    assert not adapter(tmp_path, tool(tmp_path, "uvx"), popen=broken).refresh_in_background(git_repo, tmp_path / "y")


def test_every_tool_a_hint_names_exists_in_livespec():
    named = set()
    for hint in _HINTS.values():
        named |= {w.strip(",.;()") for w in hint.split() if "_" in w or w.strip(",.;()") in {"search"}}
    named = {n for n in named if n.replace("_", "").isalpha()}
    assert named <= TOOLS, named - TOOLS


def test_hint_names_the_workspace_and_where_edits_go(tmp_path, git_repo):
    a = adapter(tmp_path)
    assert f'workspace="{git_repo.resolve()}"' in a.hint("debug-repro", git_repo, git_repo)
    assert "your edits are in /w" in a.hint("nope", git_repo, Path("/w"))


def test_test_paths():
    for path in ("tests/x.py", "pkg/test_x.py", "a/b_test.go", "web/x.test.ts", "src/__tests__/a.js"):
        assert is_test_path(path)
    assert not is_test_path("src/contest.py")


def test_the_real_schema_is_read_read_only(tmp_path, git_repo):
    db = build(git_repo)
    a = adapter(tmp_path)
    a.brief("charge_card", git_repo, a.inspect(git_repo))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM spec").fetchone()[0] == 1


def test_a_failed_index_is_remembered_until_one_succeeds_and_tried_again_by_the_next_task(tmp_path, git_repo):
    failing = adapter(tmp_path, tool(tmp_path, "uvx"), runner=lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Workspace directory not found: /old/path"))
    assert failing.last_failure(git_repo) is None
    failing.refresh(git_repo)
    assert failing.last_failure(git_repo) == "Workspace directory not found: /old/path"
    # The checkout stays where it failed: the failure still stands, and it is no reason not to try.
    assert failing.last_failure(git_repo) == "Workspace directory not found: /old/path"
    working = adapter(tmp_path, tool(tmp_path, "uvx"),
                      runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    working.refresh(git_repo)
    assert working.last_failure(git_repo) is None
    failing._failure_path(git_repo).write_text("not json")
    assert failing.last_failure(git_repo) is None


def test_the_briefing_says_how_many_symbols_the_request_reaches(tmp_path, git_repo):
    build(git_repo)
    a = adapter(tmp_path)
    status = a.inspect(git_repo)
    # parse_amount and its 22 callers
    assert a.brief("parse_amount rounds wrong", git_repo, status).reached == 23
    assert a.brief("nothing here matches zzzz", git_repo, status).reached == 0
    # no index to read says nothing, rather than zero
    assert a.brief("x", git_repo, a.inspect(tmp_path)).reached is None


def test_a_resolver_error_on_a_plugin_release_is_read_as_unpublished_never_as_a_failure(tmp_path, git_repo):
    """uv's resolver says "No solution found … livespec==X" or "there is no version of
    livespec==X": the release is not published, so the pinned one runs, and a failure an older
    cauce recorded that way is not repeated (nor retried) at that commit."""
    assert livespec_mod.unpublished_in("× No solution found when resolving tool dependencies:\n"
                                       "╰─▶ Because there is no version of livespec==0.33.0 and you require "
                                       "livespec==0.33.0, we can conclude ...") == "0.33.0"
    assert livespec_mod.unpublished_in("Because livespec==0.33.0 was not found in the package registry") == "0.33.0"
    assert livespec_mod.unpublished_in("index crashed at livespec==0.33.0") is None
    assert livespec_mod._uvx_release({"command": "/opt/uv/bin/uv", "args": ["tool", "run", "livespec@0.33.0"]}) \
        == "0.33.0"
    cfg = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / "0.33.0" / ".mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": "uvx", "args": ["livespec@0.33.0"]}}}))
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    a._remember_failure(git_repo, "× No solution found when resolving tool dependencies:\n  ╰─▶ Because there "
                                  "is no version of livespec==0.33.0 and you require livespec==0.33.0, we can ...")
    assert a.last_failure(git_repo) is None and "0.33.0" in a.unpublished()
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}
    assert not a._failure_path(git_repo).exists()
    # A plugin whose uvx is a full path off the PATH runs the pinned release with it.
    full = tmp_path / "opt" / "uvx"
    cfg.write_text(json.dumps({"mcpServers": {"livespec": {"command": str(full), "args": ["livespec@0.33.0"]}}}))
    assert adapter(tmp_path).server() == {"command": str(full), "args": [f"livespec@{PINNED}"]}
    # Any other failure is said once by the plans, while it stands.
    a._remember_failure(git_repo, "Workspace directory not found: /old")
    assert a.last_failure(git_repo) == "Workspace directory not found: /old"
    assert a.failure_said(git_repo) is False and a.failure_said(git_repo) is True
    assert a.last_failure(git_repo) == "Workspace directory not found: /old"


def plugin_manifest(tmp_path: Path, version: str, *, mcp: bool = False) -> None:
    folder = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / version
    (folder / ".claude-plugin").mkdir(parents=True)
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "livespec", "version": version}))
    if mcp:
        (folder / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"livespec": {"command": "uvx", "args": [f"livespec@{version}"]}}}))


def test_the_pin_follows_the_installed_plugins_own_manifest(tmp_path):
    assert adapter(tmp_path).pin() == PINNED
    plugin_manifest(tmp_path, "0.31.0")
    plugin_manifest(tmp_path, "0.99.0")  # newest by version, not by name order
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    assert a.pin() == "0.99.0"
    # No `.mcp.json` to run: the manifest's release is what `uvx` is given.
    assert a.server() == {"command": "uvx", "args": ["livespec@0.99.0"]}


def test_a_manifest_release_uv_found_unpublished_is_not_pinned(tmp_path):
    plugin_manifest(tmp_path, "0.99.0", mcp=True)
    a = adapter(tmp_path, tool(tmp_path, "uvx"))
    a._remember_unpublished("0.99.0")
    assert a.pin() == PINNED
    assert a.server() == {"command": "uvx", "args": [f"livespec@{PINNED}"]}


def test_a_broken_manifest_leaves_the_built_in_pin(tmp_path):
    folder = tmp_path / "cc" / "plugins" / "cache" / "rixmerz" / "livespec" / "0.40.0" / ".claude-plugin"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text("{not json")
    assert adapter(tmp_path).pin() == PINNED


def test_the_built_in_pin_is_not_behind_the_newest_livespec_plugin_installed():
    cache = Path("~/.claude/plugins/cache").expanduser()
    newest = max((livespec_mod._version_tuple(p.name) or (0,) for p in cache.glob("*/livespec/*") if p.is_dir()),
                 default=None)
    if newest is None:
        pytest.skip("no livespec plugin in this machine's plugin cache")
    assert livespec_mod._version_tuple(PINNED) >= newest, (
        f"PINNED is {PINNED} but the plugin cache holds livespec {'.'.join(map(str, newest))}: raise PINNED "
        "(and check TOOLS and the hints against that release)")


def test_a_first_index_within_a_budget_says_done_failed_large_or_pending(tmp_path, git_repo, monkeypatch):
    seen = []

    def runs(code, err=""):
        def runner(argv, **kw):
            seen.append(kw["timeout"])
            return subprocess.CompletedProcess(argv, code, "{}", err)
        return adapter(tmp_path, tool(tmp_path, "uvx"), runner=runner)

    assert runs(0).refresh_within(git_repo, 20) == ("done", "livespec index refreshed")
    assert len(seen) == 1 and 19 < seen[0] <= 20  # the run is cut at the budget, not at INDEX_TIMEOUT_S
    outcome, words = runs(1, "boom").refresh_within(git_repo, 20)
    assert outcome == "failed" and "boom" in words
    # Too many files for the seconds offered: nothing runs.
    monkeypatch.setattr(livespec_mod, "_tracked_files", lambda root: 20 * livespec_mod.FILES_PER_SECOND + 1)
    seen.clear()
    outcome, words = runs(0).refresh_within(git_repo, 20)
    assert outcome == "large" and seen == [] and "401 tracked files" in words
    # Another run holds the index and outlasts the budget: pending, the caller goes to the background.
    monkeypatch.setattr(livespec_mod, "_tracked_files", lambda root: 3)
    held = runs(0)
    with lock(held.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        assert held.refresh_within(git_repo, 1)[0] == "pending"


def test_an_index_that_finishes_in_time_gives_its_code_and_both_outputs():
    done = livespec_mod._run_in_group(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], timeout=10,
                                      env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 3
    assert done.stdout == "out\n"
    assert done.stderr == "err\n"


def _running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                           check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")  # a zombie is dead, only not reaped yet


def test_a_budget_that_runs_out_ends_the_whole_index_not_only_uvx(tmp_path, git_repo, monkeypatch):
    # `uvx` starts the `livespec index` that does the work as its child. A timeout that killed
    # only `uvx` left that child indexing with nobody holding the lock: `indexing()` then said
    # False and the background refresh started a second index on the same file.
    pidfile = tmp_path / "child.pid"
    exe = tmp_path / "bin" / "uvx"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\nPATH=/usr/bin:/bin\nsleep 60 &\necho $! > {shlex.quote(str(pidfile))}\nwait\n")
    exe.chmod(0o755)
    monkeypatch.setattr(livespec_mod, "_tracked_files", lambda root: 3)
    a = adapter(tmp_path, str(exe.parent))
    try:
        outcome, words = a.refresh_within(git_repo, 1)
        assert outcome == "pending"
        assert "did not run" in words
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 5
        while _running(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(child), "the index child outlived the budget that cut it"
        assert not a.indexing(git_repo)
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


FAKE_SERVER = '''#!{python}
import json, sys
mode = {mode!r}
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    open({log!r}, "a").write(json.dumps(message) + "\\n")
    if mode == "hang":
        continue
    if message["method"] == "initialize":
        reply = {{"protocolVersion": "2024-11-05", "capabilities": {{}}}}
    elif mode == "die" and message["params"]["name"] == "validate_openspec":
        sys.exit(0)
    elif mode == "error":
        reply = {{"content": [{{"type": "text", "text": "boom"}}], "isError": True}}
    elif message["params"]["name"] == "sync_openspec":
        print("a log line that is no message", flush=True)
        reply = {{"content": [{{"type": "text", "text": json.dumps({{"synced": 1}})}}]}}
    else:
        data = {{"valid": True, "error_count": 0, "warning_count": 1, "findings": []}}
        reply = {{"content": [{{"type": "text", "text": "[]"}}], "structuredContent": data}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": reply}}), flush=True)
'''


def fake_livespec(tmp_path: Path, mode: str = "ok") -> tuple[Livespec, Path]:
    import sys

    log = tmp_path / "asked.log"
    exe = tmp_path / "bin" / "livespec"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(FAKE_SERVER.format(python=sys.executable, mode=mode, log=str(log)))
    exe.chmod(0o755)
    return adapter(tmp_path, str(exe.parent)), log


def test_livespec_is_asked_for_validate_openspec_over_stdio_after_syncing_the_specs(tmp_path, git_repo):
    a, log = fake_livespec(tmp_path)
    assert a.validate_openspec(git_repo) == {"valid": True, "error_count": 0, "warning_count": 1, "findings": []}
    asked = [json.loads(line) for line in log.read_text().splitlines()]
    assert [m["method"] for m in asked] == ["initialize", "tools/call", "tools/call"]
    assert [m["params"]["name"] for m in asked[1:]] == ["sync_openspec", "validate_openspec"]
    assert all(m["params"]["arguments"]["workspace"] == str(git_repo.resolve()) for m in asked[1:])


@pytest.mark.parametrize(("mode", "said"), [("error", "boom"), ("die", "closed before it answered"),
                                            ("hang", "closed before it answered")])
def test_a_livespec_that_errors_dies_or_hangs_is_an_error_not_a_hang(tmp_path, git_repo, mode, said):
    a, _ = fake_livespec(tmp_path, mode)
    with pytest.raises(livespec_mod.LivespecError, match=said):
        a.call_tools(git_repo, [("validate_openspec", {})], timeout_s=2)


def test_nothing_to_ask_or_a_server_that_does_not_start_is_an_error(tmp_path, git_repo):
    with pytest.raises(livespec_mod.LivespecError, match="no livespec"):
        adapter(tmp_path).call_tools(git_repo, [("validate_openspec", {})])

    def broken(*a, **kw):
        raise OSError("no such file")

    with pytest.raises(livespec_mod.LivespecError, match="did not start"):
        adapter(tmp_path, tool(tmp_path, "livespec"), popen=broken).call_tools(git_repo, [("search", {})])


def test_validate_openspec_waits_for_the_lock_every_index_run_takes(tmp_path, git_repo):
    a, log = fake_livespec(tmp_path)
    with lock(a.lock_path(git_repo), wait_s=0) as (got, _):
        assert got
        with pytest.raises(livespec_mod.LivespecError, match="holds the index"):
            a.validate_openspec(git_repo, timeout_s=0)
    assert not log.exists()
