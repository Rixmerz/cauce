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

Checked against livespec 0.34.0. Every tool named here exists in that release;
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
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cauce import repo
from cauce.adapters import ABSENT, PRESENT, UNREADABLE, Briefing, Status
from cauce.store import home
from cauce.text import clip, words

MINIMUM_VERSION = (0, 32, 0)

#: What cauce runs when no livespec plugin is installed: the latest release known when this
#: was built, through `uvx` (`Livespec.pin` reads an installed plugin's own manifest first, and
#: a test fails when this falls behind the newest one in the plugin cache). livespec is part of
#: cauce's base, so having `uv` is enough: there is no second plugin to install.
PINNED = "0.34.0"

#: The livespec tools cauce names to a worker or calls itself. Verified to exist in 0.34.0.
TOOLS = frozenset({
    "quick_orient", "find_symbol", "who_calls", "who_does_this_call", "analyze_impact",
    "git_diff_impact", "get_symbol_source", "get_spec_implementation", "list_specs",
    "audit_coverage", "find_orphan_tests", "find_dead_code", "search",
    "sync_openspec", "validate_openspec",
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
#: How many tracked files livespec indexes in a second, as measured (372 files in about 14 s on
#: 0.28.1, cold): a repository is small enough to index before its first task when its files fit in
#: the seconds the person lets that task wait (`index_wait`).
FILES_PER_SECOND = 20

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")

Runner = Callable[..., subprocess.CompletedProcess]


class LivespecError(RuntimeError):
    """livespec could not be asked: it did not start, did not answer in time, or answered with an error."""


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def _looks_like_code(token: str) -> bool:
    """`parse_duration`, `AuthClient`, `api.routes` — not `the` or `fix`."""
    return "_" in token or "." in token or (token[:1].isalpha() and any(c.isupper() for c in token[1:]))


#: What `uv` says when a release is not on the package registry.
_NOT_PUBLISHED = re.compile(r"not found in the package registry|no version of livespec|"
                            r"no solution found when resolving", re.IGNORECASE)


#: The release an error names: uv says `livespec==X` whatever form the command took.
_NAMED_RELEASE = re.compile(r"\blivespec(?:\[[^\]]*\])?(?:==|@)([0-9][\w.+-]*)", re.IGNORECASE)


def unpublished_in(text: str) -> str | None:
    """The livespec release uv's resolver says is not on the registry ("No solution found
    when resolving tool dependencies … livespec==X", "there is no version of
    livespec==X"), or None when the text says no such thing."""
    if not _NOT_PUBLISHED.search(text or ""):
        return None
    found = _NAMED_RELEASE.search(text)
    return found.group(1).rstrip(".,;:") if found else None


def _uvx_release(server: Mapping[str, Any]) -> str | None:
    """The livespec release a `uvx livespec@X` (or `livespec==X`) entry runs, or None."""
    if Path(str(server.get("command") or "")).name not in ("uvx", "uv"):
        return None
    for arg in server.get("args") or []:
        match = re.fullmatch(r"livespec(?:\[[^\]]*\])?(?:@|==)([0-9][\w.+-]*)", str(arg))
        if match:
            return match.group(1)
    return None


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

    def _config_root(self) -> Path:
        return Path(self.env.get("CLAUDE_CONFIG_DIR") or Path(self.env.get("HOME", "~")).expanduser() / ".claude")

    def _plugin_config(self) -> tuple[dict[str, Any], str] | None:
        """The server entry of an installed livespec plugin, and its version."""
        root = self._config_root()
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

    def _plugin_release(self) -> str | None:
        """The release the newest installed livespec plugin declares in its own manifest
        (`.claude-plugin/plugin.json`), unless uv found it missing from the registry."""
        found = [(_version_tuple(m.parent.parent.name) or (0,), m)
                 for m in self._config_root().glob("plugins/cache/*/livespec/*/.claude-plugin/plugin.json")]
        for _, manifest in sorted(found, reverse=True):
            try:
                version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(version, str) and _version_tuple(version) and version not in self.unpublished():
                return version
        return None

    def pin(self) -> str:
        """The release `uvx` runs when no plugin entry and no `livespec` on PATH is used: the
        installed plugin's own, else the one this adapter was built against."""
        return self._plugin_release() or PINNED

    def _unpublished_path(self) -> Path:
        return home(self.env) / "work" / "livespec-unpublished.json"

    def unpublished(self) -> set[str]:
        """The releases `uvx` found missing from the package registry: a plugin may
        name one it was tagged with before it was published."""
        try:
            return {str(v) for v in json.loads(self._unpublished_path().read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError):
            return set()

    def _remember_unpublished(self, version: str) -> None:
        path = self._unpublished_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(self.unpublished() | {version})), encoding="utf-8")

    def server(self) -> Mapping[str, Any] | None:
        """How to run livespec: an installed plugin's own entry (unless it names a
        release `uvx` found unpublished), then a `livespec` on PATH, then the pinned
        release through `uvx`."""
        plugin = self._plugin_config()
        if plugin and _uvx_release(plugin[0]) not in self.unpublished():
            return plugin[0]
        path = self.env.get("PATH")
        if shutil.which("livespec", path=path):
            return {"command": "livespec", "args": []}
        if shutil.which("uvx", path=path):
            return {"command": "uvx", "args": [f"livespec@{self.pin()}"]}
        if plugin and _uvx_release(plugin[0]) is not None and Path(str(plugin[0]["command"])).is_absolute():
            # The plugin's own `uvx` (a full path off the PATH) runs the pinned release instead.
            command = str(plugin[0]["command"])
            run = ["tool", "run"] if Path(command).name == "uv" else []
            return {"command": command, "args": [*run, f"livespec@{self.pin()}"]}
        return plugin[0] if plugin else None

    def read_only_server(self) -> Mapping[str, Any] | None:
        """The same server behind `cauce.mcp_guard`'s `livespec` policy: calls that read the index
        pass, everything that could change it is refused. For a worker that only checks work."""
        server = self.server()
        if server is None:
            return None
        from cauce.plugins import guarded

        return guarded(server, "livespec")

    def _version(self) -> str | None:
        plugin = self._plugin_config()
        if plugin and _uvx_release(plugin[0]) not in self.unpublished():
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

    def _failure_path(self, repo_dir: Path) -> Path:
        return self.lock_path(repo_dir).with_name(self.lock_path(repo_dir).stem.replace("index", "failed") + ".json")

    def last_failure(self, repo_dir: Path) -> str | None:
        """Why the last index run failed, until one succeeds: the next task tries again, so a
        repository whose index broke (a workspace moved since, a broken config) recovers on its
        own the day the cause is gone, not only when the checkout moves."""
        try:
            data = json.loads(self._failure_path(repo_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        missing = unpublished_in(str(data.get("error") or ""))
        if missing and missing != self.pin():
            # An index that failed only because the release it ran is not published (kept by
            # a cauce that did not read uv's resolver error so): the release is remembered as
            # unpublished, the pinned one runs from now on, and the failure is no failure here.
            self._remember_unpublished(missing)
            self._remember_failure(repo_dir, None)
            return None
        return str(data.get("error") or "it failed")

    def failure_said(self, repo_dir: Path) -> bool:
        """Whether a plan already said the failure `last_failure` gives: said once, it is
        not said again in every plan while the same failure stands. The first call marks it
        said; a new failure is a new record, said once in its turn."""
        path = self._failure_path(repo_dir)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("said"):
            return True
        with contextlib.suppress(OSError):
            path.write_text(json.dumps({**data, "said": True}), encoding="utf-8")
        return False

    def _remember_failure(self, repo_dir: Path, error: str | None) -> None:
        path = self._failure_path(repo_dir)
        if error is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"error": error[:300]}), encoding="utf-8")

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
        return self._refresh(repo_dir, wait_s=wait_s)[1]

    def refresh_within(self, repo_dir: Path, budget_s: float) -> tuple[str, str]:
        """The first index of a repository, built before its task starts when it is small enough
        to take under `budget_s` seconds: (outcome, words). `done`: the index is there. `failed`: it
        ran and broke, and the failure is kept for the next task to try again. `large`: too many
        files for the budget, nothing was run. `pending`: it did not finish in time (or another
        run holds the index), so the caller indexes in the background. The run is cut at the
        budget, never left running past it."""
        files = _tracked_files(repo_dir)
        if files is not None and files > budget_s * FILES_PER_SECOND:
            return "large", (f"{files} tracked files take longer than the {budget_s:g} s a first task waits "
                             "for the index")
        outcome, said = self._refresh(repo_dir, wait_s=budget_s, budget_s=budget_s)
        return outcome, said

    def _refresh(self, repo_dir: Path, *, wait_s: float, budget_s: float | None = None) -> tuple[str, str]:
        """`refresh`'s work: (`done` | `failed` | `pending`, what to say). With a budget, the
        wait for the lock and the run together take no longer than it."""
        argv = self._index_argv(repo_dir)
        if argv is None:
            return "pending", "livespec cannot run here; the index stays as it is"
        began = time.monotonic()

        def left() -> float:
            return INDEX_TIMEOUT_S if budget_s is None else max(1.0, budget_s - (time.monotonic() - began))

        with _lock(self.lock_path(repo_dir), wait_s=wait_s) as (got, waited):
            if not got:
                return "pending", "another livespec index is still running; the index stays as it is for this task"
            if waited:
                status = self.inspect(repo_dir)
                if status.present and not status.stale:
                    return "done", "livespec index refreshed by the run this one waited for"
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=left(), check=False,
                                   env={**self.env, "CAUCE_HOOKS_OFF": "1"})
            except (OSError, subprocess.TimeoutExpired) as exc:
                return "pending", f"livespec index did not run: {exc}"
            fell_back = ""
            said = f"{proc.stderr or ''}\n{proc.stdout or ''}"
            missing = _uvx_release({"command": argv[0], "args": argv[1:]})
            if missing is None and Path(argv[0]).name in ("uvx", "uv"):
                # A release named some other way (`uv tool run`, `--from`): uv's error names it.
                missing = unpublished_in(said)
            if proc.returncode != 0 and missing and missing != self.pin() and _NOT_PUBLISHED.search(said):
                # A plugin tagged with a release not on the registry yet: every index (and
                # every worker's server) would fail until it is. The pinned one runs instead.
                self._remember_unpublished(missing)
                again = self._index_argv(repo_dir)
                if again is not None and again != argv:
                    fell_back = (f"livespec {missing} is not published, so the pinned {self.pin()} ran instead "
                                 "(and will, until the plugin names a published release)")
                    try:
                        proc = self.runner(again, capture_output=True, text=True, timeout=left(),
                                           check=False, env={**self.env, "CAUCE_HOOKS_OFF": "1"})
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        return "pending", f"{fell_back}; livespec index did not run: {exc}"
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or "").strip()[:200]
            self._remember_failure(repo_dir, error)
            return "failed", (f"{fell_back}; " if fell_back else "") + f"livespec index failed: {error}"
        self._remember_failure(repo_dir, None)
        return "done", f"livespec index refreshed ({fell_back})" if fell_back else "livespec index refreshed"

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

    def call_tools(self, repo_dir: Path, calls: Sequence[tuple[str, Mapping[str, Any]]], *,
                   timeout_s: float = 60.0) -> list[dict[str, Any]]:
        """Tools livespec has no command line for (`validate_openspec`), asked of its own server as
        any MCP client does: one session over stdio, each call's `workspace` this repository, cut at
        `timeout_s`. Raises LivespecError; the answers are the tools' own JSON objects."""
        server = self.server()
        if server is None:
            raise LivespecError("no livespec to ask")
        root = (repo.toplevel(repo_dir) or repo_dir).resolve()
        argv = [str(server["command"]), *[str(a) for a in server.get("args", [])]]
        env = {**self.env, **{str(k): str(v) for k, v in (server.get("env") or {}).items()}, "CAUCE_HOOKS_OFF": "1"}
        try:
            proc = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, env=env, cwd=str(root))
        except OSError as exc:
            raise LivespecError(f"livespec did not start: {exc}") from exc
        cut = threading.Timer(timeout_s, proc.kill)
        cut.start()
        try:
            _ask(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "cauce", "version": "0"}})
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            return [_tool_data(name, _ask(proc, n, "tools/call", {"name": name, "arguments": {
                "workspace": str(root), **arguments}})) for n, (name, arguments) in enumerate(calls, start=2)]
        except (OSError, ValueError) as exc:
            raise LivespecError(f"livespec did not answer: {exc}") from exc
        finally:
            cut.cancel()
            proc.kill()
            proc.wait()

    def validate_openspec(self, repo_dir: Path, *, strict: bool = False, timeout_s: float = 60.0) -> dict[str, Any]:
        """The structural check of the repository's OpenSpec specs, by livespec's own
        `validate_openspec`. That tool checks the index, so the specs are synced into it first
        (`sync_openspec`, livespec's own import of `openspec/`), under the lock every index run
        takes. Raises LivespecError."""
        with _lock(self.lock_path(repo_dir), wait_s=timeout_s) as (got, _):
            if not got:
                raise LivespecError("another livespec index run holds the index")
            _, result = self.call_tools(repo_dir, [("sync_openspec", {}), ("validate_openspec", {"strict": strict})],
                                        timeout_s=timeout_s)
        return result

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
        reached = len({*symbols, *(c for e in entries for c in e["caller_ids"])})
        if not entries:
            return Briefing(reached=reached)
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
        return Briefing(tuple(lines), raise_rungs, critical, tuple(dict.fromkeys(reasons)), reached)

    def symbols_named(self, text: str, status: Status) -> list[tuple[str, str]]:
        """The symbols a text names outright, as (qualified name, path): what a
        note about them is anchored to. Read-only, like everything here."""
        if not status.present:
            return []
        named = sorted({t for t in _IDENT.findall(text) if len(t) >= 4 and _looks_like_code(t)})
        found: list[tuple[str, str]] = []
        try:
            with _connect(Path(status.data["db"])) as conn:
                for token in named:
                    rows = conn.execute(
                        "SELECT s.qualified_name, f.path FROM symbol s JOIN file f ON f.id = s.file_id "
                        "WHERE f.project_id = ? AND (s.qualified_name = ? OR s.qualified_name LIKE ? OR s.name = ?) "
                        "LIMIT 3", (int(status.data["project"]), token, f"%.{token}", token.rsplit(".", 1)[-1]),
                    ).fetchall()
                    found += [(r["qualified_name"], r["path"]) for r in rows if not is_test_path(r["path"])]
        except sqlite3.Error:
            return []
        return list(dict.fromkeys(found))[:6]

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


def _ask(proc: Any, ident: int, method: str, params: Mapping[str, Any]) -> Any:
    """One JSON-RPC request to an MCP server on stdio (a message a line) and its answer."""
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise LivespecError(f"livespec closed before it answered {method}")
        try:
            message = json.loads(line)
        except ValueError:
            continue  # a log line on stdout, not a message
        if isinstance(message, dict) and message.get("id") == ident:
            if isinstance(message.get("error"), dict):
                raise LivespecError(f"{method}: {message['error'].get('message') or message['error']}")
            return message.get("result")


def _tool_data(name: str, result: Any) -> dict[str, Any]:
    """The JSON object a tool answered with (its `structuredContent`; older releases only wrap the
    same object in a text item), or the error it said."""
    if not isinstance(result, dict):
        raise LivespecError(f"{name}: no answer")
    content = result.get("content")
    text = " ".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if result.get("isError") or not isinstance(data, dict) or data.get("error"):
        fields = data if isinstance(data, dict) else {}
        said = fields.get("error") or text or "an answer that is no object"
        hint = f" ({fields['hint']})" if fields.get("hint") else ""
        raise LivespecError(clip(f"{name}: {said}{hint}", 300))
    return data


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _tracked_files(root: Path) -> int | None:
    """How many files git tracks under `root`; None when git cannot say."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.count("\0") if out.returncode == 0 else None


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
        "caller_ids": sorted({r["id"] for r in callers}),
        "specs": [dict(s) for s in specs],
        "tests": tests,
    }
