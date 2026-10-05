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
