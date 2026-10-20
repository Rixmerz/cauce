from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from pathlib import Path

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


def test_a_failed_index_is_not_tried_again_until_the_checkout_moves(tmp_path, git_repo):
    failing = adapter(tmp_path, tool(tmp_path, "uvx"), runner=lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, "", "Workspace directory not found: /old/path"))
    assert failing.last_failure(git_repo) is None
    failing.refresh(git_repo)
    assert failing.last_failure(git_repo) == "Workspace directory not found: /old/path"
    (git_repo / "next.py").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=git_repo, check=True)
    assert failing.last_failure(git_repo) is None  # the checkout moved: worth one more try
    failing.refresh(git_repo)
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
