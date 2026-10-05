"""livespec, adopted: the code index the core reads before it routes, briefs and reports.

livespec keeps its index in ``<repo>/.mcp-docs/docs.db`` (or a shared file
named by ``[workspace] group_db`` in ``.livespec.toml``): symbols, a call graph,
code chunks under full-text search, and the links between symbols and specs.
That file is readable with ``sqlite3`` in read-only mode, so the core uses it
without a token and without an MCP call:

- **routing** — a task whose code is linked to a critical spec, or whose
  symbol is called from everywhere, starts one rung up;
- **briefing** — the worker's prompt opens with a code map: the symbols the
  request is about, where they are, how many places call them, the specs they
  implement and the tests that exercise them;
- **reporting** — after a pass, which specs the change touched and which
  callers in other files the change did not touch;
- **freshness** — an index older than the last commit is refreshed with
  livespec's own headless CLI (``livespec index``) before any of the above.

The worker also gets livespec's MCP server, with a hint per kind of work that
names the calls that kind needs and the ``workspace`` every call requires.

Checked against livespec 0.32.0. Every tool named here exists in that release;
`TOOLS` is the pinned list, and a test holds every hint to it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cauce import repo
from cauce.adapters import ABSENT, PRESENT, UNREADABLE, Briefing, Status
from cauce.store import home
from cauce.text import words

MINIMUM_VERSION = (0, 32, 0)

#: What cauce runs when no livespec is installed: the release this adapter was
#: checked against, through `uvx`. livespec is part of cauce's base, so having
#: `uv` is enough — there is no second plugin to install.
PINNED = "0.32.0"

#: The livespec tools cauce names to a worker. Verified to exist in 0.32.0.
TOOLS = frozenset({
    "quick_orient", "find_symbol", "who_calls", "who_does_this_call", "analyze_impact",
    "git_diff_impact", "get_symbol_source", "get_spec_implementation", "list_specs",
    "audit_coverage", "find_orphan_tests", "find_dead_code", "search",
})

_HINTS: Mapping[str, str] = {
    "debug-repro": "find the failing symbol (find_symbol), then who_calls and analyze_impact before changing it",
    "debug-unclear": "map the path first: who_calls, who_does_this_call and analyze_impact on every suspect",
    "implement": "before editing a symbol, who_calls and analyze_impact; get_spec_implementation for the spec "
                 "it serves",
    "feature": "list_specs and get_spec_implementation for what exists; analyze_impact on each symbol you change",
    "refactor": "who_calls on every symbol you move or rename; find_dead_code for what can go",
    "ui": "find_symbol for the component; who_calls to see where it renders",
    "test": "audit_coverage and find_orphan_tests to see what is untested; get_symbol_source to read the target",
    "review-routine": "git_diff_impact on the change; analyze_impact on anything it touches",
    "review-critical": "git_diff_impact on the change; analyze_impact and get_spec_implementation for every "
                       "spec it touches",
    "explore": "quick_orient for the overview, then search and find_symbol",
    "plan": "quick_orient, list_specs and analyze_impact on the parts the plan changes",
}

#: Spec priority that makes a task critical.
_CRITICAL_PRIORITIES = frozenset({"critical"})
#: Callers above which a symbol is load-bearing enough to start one rung up.
WIDELY_USED = 15
MAP_SYMBOLS = 6
INDEX_TIMEOUT_S = 600

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")

Runner = Callable[..., subprocess.CompletedProcess]


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def _looks_like_code(token: str) -> bool:
    """`parse_duration`, `AuthClient`, `api.routes` — not `the` or `fix`."""
    return "_" in token or "." in token or (token[:1].isalpha() and any(c.isupper() for c in token[1:]))


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    found = re.findall(r"\d+", text)
    return tuple(int(n) for n in found[:3]) if found else None


class Livespec:
    name = "livespec"

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.env = dict(os.environ) if env is None else dict(env)
        self.runner = runner
        self.popen = popen

    # --- where livespec is ------------------------------------------------

    def _plugin_config(self) -> tuple[dict[str, Any], str] | None:
        """The server entry of an installed livespec plugin, and its version."""
        root = Path(self.env.get("CLAUDE_CONFIG_DIR") or Path(self.env.get("HOME", "~")).expanduser() / ".claude")
        found = []
        for cfg in root.glob("plugins/cache/*/livespec/*/.mcp.json"):
            found.append((_version_tuple(cfg.parent.name) or (0,), cfg))
        for _, cfg in sorted(found, reverse=True):
            try:
                server = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["livespec"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(server, dict) and server.get("command"):
                return server, cfg.parent.name
        return None

    def server(self) -> Mapping[str, Any] | None:
        """How to run livespec: an installed plugin's own entry, then a `livespec`
        on PATH, then the pinned release through `uvx`."""
        plugin = self._plugin_config()
        if plugin:
            return plugin[0]
        path = self.env.get("PATH")
        if shutil.which("livespec", path=path):
            return {"command": "livespec", "args": []}
        if shutil.which("uvx", path=path):
            return {"command": "uvx", "args": [f"livespec@{PINNED}"]}
        return None

    def _version(self) -> str | None:
        plugin = self._plugin_config()
        if plugin:
            return plugin[1]
        server = self.server()
        for arg in (server or {}).get("args", []):
            if isinstance(arg, str) and arg.startswith("livespec@"):
                return arg.split("@", 1)[1]
        return None

    # --- the index --------------------------------------------------------

    @staticmethod
    def db_path(root: Path) -> Path:
        config = root / ".livespec.toml"
        if config.is_file():
            try:
                raw = tomllib.loads(config.read_text(encoding="utf-8")).get("workspace", {}).get("group_db")
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, str) and raw.strip():
                path = Path(raw).expanduser()
                return path if path.is_absolute() else (root / path).resolve()
        return root / ".mcp-docs" / "docs.db"

    def inspect(self, repo_dir: Path) -> Status:
        root = repo.toplevel(repo_dir) or repo_dir
        root = root.resolve()
        version = self._version()
        db = self.db_path(root)
        if not db.is_file():
            runnable = self.server() is not None
            detail = ("this repository is not indexed yet" if runnable
                      else "cannot run: no livespec, and no `uvx` to run the pinned release")
            return Status(self.name, ABSENT, detail, version)
        try:
            with _connect(db) as conn:
                row = conn.execute("SELECT id FROM project WHERE root = ? ORDER BY id LIMIT 1",
                                   (str(root),)).fetchone()
                if row is None:
                    return Status(self.name, ABSENT, f"the index at {db} has no project for {root}", version)
                project = int(row[0])
                run = conn.execute(
                    "SELECT finished_at FROM index_run WHERE project_id = ? AND finished_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1", (project,)).fetchone()
        except sqlite3.Error as exc:
            return Status(self.name, UNREADABLE, f"{db}: {exc}", version)
        indexed_at = run[0] if run else None
        stale = _older_than_head(indexed_at, root)
        detail = f"indexed {indexed_at} UTC" if indexed_at else "never finished an index run"
        floor = _version_tuple(version)
        if floor is not None and floor < MINIMUM_VERSION:
            checked = ".".join(map(str, MINIMUM_VERSION))
            detail += f"; v{version} predates the {checked} this adapter was checked against"
        return Status(self.name, PRESENT, detail, version, stale,
                      {"db": str(db), "project": project, "root": str(root)})

    def _index_argv(self, repo_dir: Path) -> list[str] | None:
        server = self.server()
        if server is None:
            return None
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        return [str(server["command"]), *[str(a) for a in server.get("args", [])], "index", str(root)]

    def lock_path(self, repo_dir: Path) -> Path:
        """One lock per index file: two `livespec index` runs on the same file
        collide ("database is locked"), and a shared group index is one file for
        several repositories."""
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        digest = hashlib.sha256(str(self.db_path(root)).encode()).hexdigest()[:16]
        return home(self.env) / "work" / f"livespec-index-{digest}.lock"

    def indexing(self, repo_dir: Path) -> bool:
        """Whether a refresh cauce started holds this index now."""
        with _lock(self.lock_path(repo_dir), wait_s=0) as (got, _):
            return not got

    def refresh(self, repo_dir: Path, *, wait_s: float = INDEX_TIMEOUT_S) -> str:
        """`livespec index` through livespec's own CLI: a first index, or an
        incremental one. Costs time, never tokens.

        Never two at once on one index. The session-start refresh, and parallel
        tasks each planning in the same repository, would otherwise start one
        apiece and all but one fail. A refresh that finds another running waits
        for it, and then runs only if the index is still behind.
        """
        argv = self._index_argv(repo_dir)
        if argv is None:
            return "livespec cannot run here; the index stays as it is"
        with _lock(self.lock_path(repo_dir), wait_s=wait_s) as (got, waited):
            if not got:
                return "another livespec index is still running; the index stays as it is for this task"
            if waited:
                status = self.inspect(repo_dir)
                if status.present and not status.stale:
                    return "livespec index refreshed by the run this one waited for"
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=INDEX_TIMEOUT_S, check=False,
                                   env={**self.env, "CAUCE_HOOKS_OFF": "1"})
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"livespec index did not run: {exc}"
        if proc.returncode != 0:
            return f"livespec index failed: {(proc.stderr or proc.stdout or '').strip()[:200]}"
        return "livespec index refreshed"

    def refresh_in_background(self, repo_dir: Path, log: Path) -> bool:
        """Start an index refresh and return at once — for a hook, which must not
        wait minutes on a large repository. It runs as `cauce index-livespec`, so
        it holds the same lock as every other refresh; none starts while one runs."""
        if self._index_argv(repo_dir) is None or self.indexing(repo_dir):
            return False
        from cauce import dispatch

        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as out:
                self.popen([sys.executable, "-m", "cauce", "index-livespec", str(root)], stdout=out, stderr=out,
                           stdin=subprocess.DEVNULL, env={**dispatch.child_env(self.env), "CAUCE_HOOKS_OFF": "1"},
                           start_new_session=True)
        except OSError:
            return False
        return True

    # --- before the first attempt ------------------------------------------

    def brief(self, text: str, repo_dir: Path, status: Status) -> Briefing:
        if not status.present:
            return Briefing()
        try:
            with _connect(Path(status.data["db"])) as conn:
                symbols = _matching_symbols(conn, int(status.data["project"]), text)
                entries = [_describe(conn, sid) for sid in symbols]
        except sqlite3.Error as exc:
            return Briefing(reasons=(f"livespec index unreadable while briefing: {exc}",))
        if not entries:
            return Briefing()
        lines = [f"Code map from livespec ({status.detail}). Read it before searching:"]
        raise_rungs, critical, reasons = 0, False, []
        for e in entries:
            line = f"- `{e['qname']}` ({e['kind']}) {e['path']}:{e['line']} — {e['callers']} caller(s)"
            if e["specs"]:
                line += "; specs: " + ", ".join(f"{s['spec_id']} {s['title']} [{s['priority']}]"
                                                for s in e["specs"][:3])
            if e["tests"]:
                line += "; tests: " + ", ".join(e["tests"][:3])
            lines.append(line)
            if any(s["priority"] in _CRITICAL_PRIORITIES for s in e["specs"]):
                critical = True
                reasons.append(f"livespec: {e['qname']} implements a critical spec")
            elif e["callers"] >= WIDELY_USED:
                reasons.append(f"livespec: {e['qname']} has {e['callers']} callers")
        if reasons:
            raise_rungs = 1
        return Briefing(tuple(lines), raise_rungs, critical, tuple(dict.fromkeys(reasons)))

    # --- after a pass ------------------------------------------------------

    def assess(self, changed: Sequence[str], repo_dir: Path, status: Status) -> tuple[str, ...]:
        if not status.present or not changed:
            return ()
        changed_set = set(changed)
        try:
            with _connect(Path(status.data["db"])) as conn:
                project = int(status.data["project"])
                marks = ", ".join("?" for _ in changed_set)
                ids = [r[0] for r in conn.execute(
                    f"SELECT s.id FROM symbol s JOIN file f ON f.id = s.file_id "  # noqa: S608 - placeholders only
                    f"WHERE f.project_id = ? AND f.path IN ({marks})", [project, *changed_set])]
                if not ids:
                    return ()
                id_marks = ", ".join("?" for _ in ids)
                specs = conn.execute(
                    f"SELECT DISTINCT sp.spec_id, sp.title, sp.priority FROM spec_symbol ss "  # noqa: S608
                    f"JOIN spec sp ON sp.id = ss.spec_id WHERE ss.symbol_id IN ({id_marks}) "
                    f"ORDER BY sp.spec_id", ids).fetchall()
                callers = conn.execute(
                    f"SELECT DISTINCT f.path FROM symbol_edge e JOIN symbol s ON s.id = e.src_symbol_id "  # noqa: S608
                    f"JOIN file f ON f.id = s.file_id WHERE e.edge_type = 'calls' "
                    f"AND e.dst_symbol_id IN ({id_marks}) ORDER BY f.path", ids).fetchall()
        except sqlite3.Error as exc:
            return (f"livespec: could not assess the change ({exc})",)
        lines = []
        if specs:
            lines.append("livespec: the change touches specs " + ", ".join(
                f"{s[0]} {s[1]} [{s[2]}]" for s in specs[:8]))
        outside = [p for (p,) in callers if p not in changed_set]
        code = [p for p in outside if not is_test_path(p)]
        tests = [p for p in outside if is_test_path(p)]
        if code:
            lines.append(f"livespec: {len(code)} file(s) outside the change call what it changed: "
                         + ", ".join(code[:6]) + (" …" if len(code) > 6 else ""))
        if tests:
            lines.append("livespec: tests that exercise the changed code: " + ", ".join(tests[:6]))
        return tuple(lines)

    # --- for the worker ------------------------------------------------------

    def hint(self, kind: str, repo_dir: Path, workdir: Path) -> str:
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        what = _HINTS.get(kind, _HINTS["implement"])
        where = f'pass workspace="{root}" on every call'
        if workdir.resolve() != root:
            where += f"; the index describes the base commit there, your edits are in {workdir}"
        return f"{what}; {where}."


# --- queries ------------------------------------------------------------------


@contextlib.contextmanager
def _lock(path: Path, *, wait_s: float, pause: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Iterator[tuple[bool, bool]]:
    """Yields (held, waited): an exclusive lock on `path`, waiting up to `wait_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, waited = clock() + wait_s, False
    with open(path, "a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if clock() >= deadline:
                    yield False, waited
                    return
                waited = True
                pause(1.0)
        try:
            yield True, waited
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _older_than_head(indexed_at: str | None, root: Path) -> bool:
    if not indexed_at:
        return True
    try:
        stamp = datetime.fromisoformat(indexed_at.replace(" ", "T")).replace(tzinfo=UTC)
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=str(root), capture_output=True,
                             text=True, timeout=10, check=False).stdout.strip()
        return bool(out) and datetime.fromtimestamp(int(out), UTC) > stamp
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False


def _matching_symbols(conn: sqlite3.Connection, project: int, text: str) -> list[int]:
    """Symbols the request names outright, then the best full-text matches —
    production code first; tests reach the map through the symbols they call."""
    picked: list[int] = []
    named = {t for t in _IDENT.findall(text) if len(t) >= 4 and _looks_like_code(t)}
    for token in sorted(named):
        rows = conn.execute(
            "SELECT s.id, f.path FROM symbol s JOIN file f ON f.id = s.file_id WHERE f.project_id = ? "
            "AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ?) LIMIT 5",
            (project, token.rsplit(".", 1)[-1], token, f"%.{token}"),
        ).fetchall()
        picked += [r["id"] for r in rows if not is_test_path(r["path"]) and r["id"] not in picked]
    terms = [w.replace('"', "") for w in words(text)][:12]
    if terms and len(picked) < MAP_SYMBOLS:
        rows = conn.execute(
            "SELECT c.source_id AS id, c.file_path AS path FROM chunk_fts JOIN chunk c ON c.id = chunk_fts.rowid "
            "WHERE chunk_fts MATCH ? AND c.project_id = ? AND c.source_type = 'symbol' "
            "ORDER BY bm25(chunk_fts) LIMIT 60",
            (" OR ".join(f'"{t}"' for t in terms), project),
        ).fetchall()
        for r in rows:
            if r["id"] is not None and not is_test_path(r["path"] or "") and r["id"] not in picked:
                picked.append(int(r["id"]))
    return picked[:MAP_SYMBOLS]


def _describe(conn: sqlite3.Connection, symbol_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.qualified_name, s.kind, s.start_line, f.path FROM symbol s JOIN file f ON f.id = s.file_id "
        "WHERE s.id = ?", (symbol_id,)).fetchone()
    callers = conn.execute(
        "SELECT DISTINCT f.path, s.id FROM symbol_edge e JOIN symbol s ON s.id = e.src_symbol_id "
        "JOIN file f ON f.id = s.file_id WHERE e.dst_symbol_id = ? AND e.edge_type = 'calls'",
        (symbol_id,)).fetchall()
    specs = conn.execute(
        "SELECT sp.spec_id, sp.title, sp.priority FROM spec_symbol ss JOIN spec sp ON sp.id = ss.spec_id "
        "WHERE ss.symbol_id = ? AND ss.relation = 'implements' ORDER BY sp.spec_id", (symbol_id,)).fetchall()
    tests = sorted({r["path"] for r in callers if is_test_path(r["path"])})
    return {
        "qname": row["qualified_name"], "kind": row["kind"], "line": row["start_line"], "path": row["path"],
        "callers": len({r["id"] for r in callers}),
        "specs": [dict(s) for s in specs],
        "tests": tests,
    }
