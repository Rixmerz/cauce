"""The memory: tasks, the messages coupled to them, every attempt, and what failed.

One SQLite file per machine, shared by every repository on it. That is the
point of it being one file: a fix that failed in one checkout is a dead end
worth knowing about in the next one, and a task kind that always lands a rung
above its start in every repo is a ladder that starts too low.

Four things are kept:

- **tasks** — a unit of work, whether it arrived as a prompt in a session or
  was dispatched by `cauce run`;
- **messages** — what was said about a task, in order: the prompt, every
  follow-up typed while it ran, the result. A follow-up belongs to the task it
  was typed into, not to a task of its own;
- **attempts** — each worker run on a task: the cell, the turns, what it cost,
  how it failed and which way the router moved next. This is the reroute trace,
  and the router reads it back to choose where the next task of the same kind
  starts;
- **problems and fixes** — what was tried against a failure, and whether it
  worked. Searched across repositories, so a dead end is not rediscovered.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cauce.text import fold, title_of, words

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, cwd TEXT, repo TEXT, started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, parent_id INTEGER, repo TEXT, cwd TEXT,
  title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  kind TEXT, complexity TEXT, class_source TEXT, class_reason TEXT,
  status TEXT NOT NULL, source TEXT NOT NULL, prompt_id TEXT,
  result TEXT, start_cell TEXT, final_cell TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_session ON tasks (session_id, status);
CREATE INDEX IF NOT EXISTS tasks_kind ON tasks (kind, status);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, role TEXT NOT NULL,
  text TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_task ON messages (task_id);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  cell TEXT NOT NULL, max_turns INTEGER NOT NULL, passed INTEGER NOT NULL,
  failure TEXT, summary TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
  cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
  duration_s REAL NOT NULL DEFAULT 0, changed_paths TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '[]',
  move TEXT, move_reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts (task_id, seq);
CREATE TABLE IF NOT EXISTS problems (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, title TEXT NOT NULL,
  symptom TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, problem_id INTEGER NOT NULL, repo TEXT,
  description TEXT NOT NULL, outcome TEXT NOT NULL, why TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '', task_id INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fixes_problem ON fixes (problem_id);
CREATE TABLE IF NOT EXISTS usage (
  message_id TEXT PRIMARY KEY, session_id TEXT, task_id INTEGER, repo TEXT, model TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  sidechain INTEGER NOT NULL DEFAULT 0, ts TEXT
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
CREATE TABLE IF NOT EXISTS transcript_offsets (path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tool_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, task_id INTEGER, attempt INTEGER, ts TEXT NOT NULL,
  tool TEXT, sig TEXT NOT NULL, arg_hash TEXT NOT NULL, ok INTEGER NOT NULL, cwd TEXT
);
CREATE INDEX IF NOT EXISTS tool_events_ts ON tool_events (ts);
CREATE INDEX IF NOT EXISTS tool_events_task ON tool_events (task_id, attempt);
CREATE TABLE IF NOT EXISTS habits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT, steps TEXT NOT NULL, command TEXT NOT NULL,
  trigger_suffix TEXT NOT NULL, settings_path TEXT NOT NULL, installed_at TEXT NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, disabled_at TEXT,
  removed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lanes (
  repo TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  kind UNINDEXED, ref_id UNINDEXED, repo UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);
"""

#: Words a query and a problem must share for a strict match.
STRICT_OVERLAP = 3

TASK_STATES = ("queued", "running", "done", "failed", "interrupted", "blocked", "replan", "needs_approval",
               "cancelled")

#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` never
#: alters a table that exists, so each one is added here when it is missing.
#: Append-only: a column is never renamed or removed once a database has it.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tasks": {
        "pid": "INTEGER",  # the `cauce run` process driving it, while it runs
        "current_cell": "TEXT",  # the cell of the attempt in flight
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        # A cell a person chose. The task ran there because they said so, which
        # says nothing about where the router should start the next one.
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        # What a queued task is run with: budget, verify command, kind, start.
        "options": "TEXT NOT NULL DEFAULT '{}'",
        # Set when the dispatcher, not a person, started the run: only those
        # pause their lane when they fail.
        "dispatched": "INTEGER NOT NULL DEFAULT 0",
        # Whether a queued task may start beside the work already going in its
        # repository (1) or waits its turn (0), and why. NULL: not decided yet.
        "parallel": "INTEGER",
        "parallel_reason": "TEXT",
    },
    "problems": {
        "first_seen": "TEXT",
        "last_seen": "TEXT",
    },
    "fixes": {
        # The window a fix was trusted in. A fix that worked and later stopped
        # holding is the most valuable dead end there is: it looked solved.
        "believed_from": "TEXT",
        "invalidated_on": "TEXT",
        # Evidence a person can check: the commit the verdict is about.
        "commit_sha": "TEXT",
    },
}
FIX_OUTCOMES = ("worked", "failed", "partial", "pending")
PROBLEM_STATES = ("open", "solved", "recurring")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def home(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    if env.get("CAUCE_HOME"):
        return Path(env["CAUCE_HOME"]).expanduser()
    xdg = env.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "share"
    return base / "cauce"


SETUP_RETRIES = 100


@contextlib.contextmanager
def _setup_lock(db: Path) -> Iterator[None]:
    """An exclusive lock on `<db>.lock` where the platform has one; a no-op elsewhere."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        yield
        return
    with open(f"{db}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_SESSION_COLUMNS = (
    "(SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id AND t.source = 'hook') AS prompts, "
    "(SELECT t.title FROM tasks t WHERE t.session_id = s.id AND t.source = 'hook' "
    " ORDER BY t.id DESC LIMIT 1) AS last_prompt, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.session_id = s.id AND t.status = 'running') AS running, "
    "(SELECT COALESCE(SUM(t.cost_usd), 0) FROM tasks t WHERE t.session_id = s.id) AS cost_usd"
)


def _in(column: str, values: Iterable[str] | None) -> tuple[str | None, list[str]]:
    """A WHERE clause keeping `column` in `values`: ("", []) for no filter, (None, []) for an
    empty one, which matches nothing."""
    if values is None:
        return "", []
    values = list(values)
    if not values:
        return None, []
    return f"WHERE {column} IN ({', '.join('?' * len(values))})", values


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._setup()
        except BaseException:
            self._conn.close()
            raise

    def _setup(self) -> None:
        """WAL, the schema and the added columns, one opener at a time.

        Switching a file to WAL and altering a table take locks SQLite may refuse
        at once instead of waiting for (a hook, the dispatcher and the UI often
        open the file in the same instant). A lock file beside the database
        queues the openers; a refusal that still gets through is retried.
        """
        with _setup_lock(self.path):
            for attempt in range(SETUP_RETRIES):
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.executescript(SCHEMA)
                    self._migrate()
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == SETUP_RETRIES - 1:
                        raise
                    time.sleep(0.02 + random.random() * 0.05)  # noqa: S311 - jitter, not security

    def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column in have:
                    continue
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError as exc:
                    # Another process or thread opened the file at the same moment
                    # and added it first: the column is there, which is the point.
                    if "duplicate column" not in str(exc):
                        raise

    @classmethod
    def open(cls, env: dict[str, str] | None = None) -> Store:
        return cls(home(env) / "cauce.db")

    def close(self) -> None:
        self._conn.close()

    # --- sessions --------------------------------------------------------

    def touch_session(self, session_id: str, cwd: str | None, repo: str | None) -> None:
        stamp = now()
        self._conn.execute(
            "INSERT INTO sessions (id, cwd, repo, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cwd = COALESCE(excluded.cwd, cwd), "
            "repo = COALESCE(excluded.repo, repo), last_seen_at = excluded.last_seen_at",
            (session_id, cwd, repo, stamp, stamp),
        )

    def get_session(self, session_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def sessions(self, *, repos: Iterable[str] | None = None, limit: int = 100) -> list[dict]:
        """Claude Code sessions cauce has seen, latest first, each with how many prompts it
        took, the last one, and whether one is still running."""
        scope, params = _in("s.repo", repos)
        if scope is None:
            return []
        query = f"SELECT s.*, {_SESSION_COLUMNS} FROM sessions s {scope} ORDER BY s.last_seen_at DESC LIMIT ?"  # noqa: S608
        rows = self._conn.execute(query, [*params, limit]).fetchall()
        return [dict(r) for r in rows]

    def projects(self) -> list[dict]:
        """Every repository cauce has worked in, from its tasks and the sessions it saw: the
        directory it was last used from, when, and how its tasks stand."""
        rows = self._conn.execute(
            "WITH seen AS ("
            " SELECT repo, cwd, updated_at AS at FROM tasks WHERE repo IS NOT NULL AND cwd IS NOT NULL "
            "   AND source != 'delegation' "
            " UNION ALL SELECT repo, cwd, last_seen_at FROM sessions WHERE repo IS NOT NULL AND cwd IS NOT NULL) "
            "SELECT repo, MAX(at) AS last_seen, "
            " (SELECT cwd FROM seen s2 WHERE s2.repo = seen.repo ORDER BY at DESC LIMIT 1) AS cwd "
            "FROM seen GROUP BY repo ORDER BY last_seen DESC"
        ).fetchall()
        out = []
        for r in rows:
            counts = dict(self._conn.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE repo = ? AND source IN ('cauce', 'queue') GROUP BY status",
                (r["repo"],)).fetchall())
            sessions = self._conn.execute("SELECT COUNT(*) FROM sessions WHERE repo = ?", (r["repo"],)).fetchone()[0]
            out.append({**dict(r), "tasks": counts, "sessions": int(sessions)})
        return out

    # --- tasks -----------------------------------------------------------

    def create_task(self, body: str, *, status: str, source: str, **fields: Any) -> dict:
        if status not in TASK_STATES:
            raise ValueError(f"unknown task status {status!r}")
        stamp = now()
        row = {
            "title": fields.pop("title", None) or title_of(body),
            "body": body,
            "status": status,
            "source": source,
            "created_at": stamp,
            "updated_at": stamp,
            **fields,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = self._conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(row.values()))  # noqa: S608
        task_id = int(cur.lastrowid)
        self.add_message(task_id, "user", body)
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and fields["status"] not in TASK_STATES:
            raise ValueError(f"unknown task status {fields['status']!r}")
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*fields.values(), task_id])  # noqa: S608

    def list_tasks(
        self,
        *,
        repo: str | None = None,
        status: Sequence[str] | None = None,
        session_id: str | None = None,
        source: Sequence[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
        if source:
            clauses.append(f"source IN ({', '.join('?' for _ in source)})")
            params.extend(source)
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if status:
            clauses.append(f"status IN ({', '.join('?' for _ in status)})")
            params.extend(status)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", [*params, limit]  # noqa: S608
        ).fetchall()
        return [dict(r) for r in rows]

    def running_task(self, session_id: str, prompt_id: str | None = None) -> dict | None:
        if prompt_id is not None:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND prompt_id = ? AND status = 'running' "
                "ORDER BY id DESC LIMIT 1",
                (session_id, prompt_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND status = 'running' AND source = 'hook' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def delegation(self, session_id: str, tool_use_id: str | None) -> dict | None:
        """The running delegation a tool call opened; by its id when the event
        carries one, else the session's newest."""
        if tool_use_id:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND source = 'delegation' AND prompt_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id, tool_use_id)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND source = 'delegation' AND status = 'running' "
                "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row) if row else None

    def last_prompt_task(self, session_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE session_id = ? AND source = 'hook' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def interrupt_running(self, session_id: str, exclude: Iterable[int] = ()) -> int:
        skip = list(exclude)
        extra = f" AND id NOT IN ({', '.join('?' for _ in skip)})" if skip else ""
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'interrupted', updated_at = ? "  # noqa: S608
            "WHERE session_id = ? AND status = 'running' AND source = 'hook'" + extra,
            [now(), session_id, *skip],
        )
        return cur.rowcount

    # --- messages --------------------------------------------------------

    def add_message(self, task_id: int, role: str, text: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (task_id, role, text, ts) VALUES (?, ?, ?, ?)",
            (task_id, role, text, now()),
        )

    def messages(self, task_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, text, ts FROM messages WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- attempts: the reroute trace -------------------------------------

    def add_attempt(self, task_id: int, **fields: Any) -> int:
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        for key in ("changed_paths", "capabilities"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(list(fields[key]))
        row = {"task_id": task_id, "seq": seq, "created_at": now(), **fields}
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._conn.execute(f"INSERT INTO attempts ({cols}) VALUES ({marks})", list(row.values()))  # noqa: S608
        self._conn.execute(
            "UPDATE tasks SET cost_usd = cost_usd + ?, updated_at = ? WHERE id = ?",
            (float(fields.get("cost_usd") or 0), now(), task_id),
        )
        return int(seq)

    def set_move(self, task_id: int, seq: int, move: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE attempts SET move = ?, move_reason = ? WHERE task_id = ? AND seq = ?",
            (move, reason, task_id, seq),
        )

    def attempts(self, task_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def landings(self, kind: str, *, repo: str | None = None, limit: int = 20) -> list[str]:
        """Where recent finished tasks of this kind passed, newest first.

        Only tasks the core ran count: a prompt answered in a session has no
        cell, and guessing one would teach the router a number nobody measured.
        """
        scope = " AND t.repo = ?" if repo is not None else ""
        params: list[Any] = [kind, *([repo] if repo is not None else []), limit]
        rows = self._conn.execute(
            "SELECT t.final_cell FROM tasks t WHERE t.kind = ? AND t.status = 'done' "  # noqa: S608
            "AND t.final_cell IS NOT NULL AND t.source = 'cauce' AND t.pinned = 0" + scope
            + " ORDER BY t.id DESC LIMIT ?",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    # --- the queue and its lanes ---------------------------------------------

    def enqueue(self, body: str, *, repo: str | None, cwd: str | None, session_id: str | None = None,
                options: dict | None = None) -> dict:
        return self.create_task(body, status="queued", source="queue", repo=repo, cwd=cwd,
                                session_id=session_id, options=json.dumps(options or {}))

    def queued_in(self, repo: str | None) -> list[dict]:
        """A lane's queued tasks, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'queued' AND repo IS ? ORDER BY id", (repo,)).fetchall()
        return [dict(r) for r in rows]

    def dispatched_running(self, repo: str | None) -> list[dict]:
        """What the dispatcher has running in a lane now."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' AND dispatched = 1 AND repo IS ? ORDER BY id",
            (repo,)).fetchall()
        return [dict(r) for r in rows]

    def set_parallel(self, task_id: int, parallel: bool, reason: str) -> None:
        self.update_task(task_id, parallel=int(parallel), parallel_reason=reason)

    def claim(self, task_id: int) -> bool:
        """Move a queued task to running, once: two dispatchers never run one task."""
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'running', dispatched = 1, updated_at = ? "
            "WHERE id = ? AND status = 'queued'", (now(), task_id))
        return cur.rowcount == 1

    def pause_lane(self, repo: str | None, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO lanes (repo, paused, reason, updated_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET paused = 1, reason = excluded.reason, updated_at = excluded.updated_at",
            (repo or "", reason, now()))

    def unpause_lane(self, repo: str | None) -> None:
        self._conn.execute(
            "INSERT INTO lanes (repo, paused, reason, updated_at) VALUES (?, 0, NULL, ?) "
            "ON CONFLICT(repo) DO UPDATE SET paused = 0, reason = NULL, updated_at = excluded.updated_at",
            (repo or "", now()))

    def lanes(self) -> list[dict]:
        """Every repository with queued work or a lane record, and its state."""
        rows = self._conn.execute(
            "SELECT r.repo, COALESCE(l.paused, 0) AS paused, l.reason, "
            "(SELECT count(*) FROM tasks q WHERE q.repo IS r.repo AND q.status = 'queued') AS queued "
            "FROM (SELECT DISTINCT repo FROM tasks WHERE status = 'queued' UNION SELECT repo FROM lanes) r "
            "LEFT JOIN lanes l ON l.repo = r.repo ORDER BY r.repo"
        ).fetchall()
        return [dict(r) for r in rows]

    def children(self, task_id: int, status: Sequence[str] | None = None) -> list[dict]:
        clause = f" AND status IN ({', '.join('?' for _ in status)})" if status else ""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE parent_id = ?" + clause + " ORDER BY id",  # noqa: S608
            [task_id, *(status or [])]).fetchall()
        return [dict(r) for r in rows]

    # --- spend -----------------------------------------------------------------

    def add_usage(self, **record: Any) -> None:
        cols = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        self._conn.execute(f"INSERT OR REPLACE INTO usage ({cols}) VALUES ({marks})",  # noqa: S608
                           list(record.values()))

    def transcript_offset(self, path: str) -> int:
        row = self._conn.execute("SELECT offset FROM transcript_offsets WHERE path = ?", (path,)).fetchone()
        return int(row[0]) if row else 0

    def set_transcript_offset(self, path: str, offset: int) -> None:
        self._conn.execute(
            "INSERT INTO transcript_offsets (path, offset) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset", (path, offset))

    def usage_by_model(self, *, days: int = 7, repo: str | None = None) -> list[dict]:
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT model, count(*) AS messages, sum(input_tokens) AS input_tokens, "  # noqa: S608
            "sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write "
            f"FROM usage WHERE ts >= datetime('now', ?) {scope} GROUP BY model ORDER BY output_tokens DESC",
            [f"-{int(days)} days", *params]).fetchall()
        return [dict(r) for r in rows]

    def worker_spend(self, *, days: int = 7, repo: str | None = None) -> list[dict]:
        """What workers cost, by cell: attempts, passes, dollars, tokens."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT a.cell, count(*) AS attempts, sum(a.passed) AS passed, round(sum(a.cost_usd), 4) AS usd, "  # noqa: S608
            "sum(a.input_tokens) AS input_tokens, sum(a.output_tokens) AS output_tokens "
            "FROM attempts a JOIN tasks t ON t.id = a.task_id "
            f"WHERE a.created_at >= ? {scope} GROUP BY a.cell ORDER BY usd DESC",
            [_days_ago(days), *params]).fetchall()
        return [dict(r) for r in rows]

    def spend_by_day(self, *, days: int = 14) -> list[dict]:
        """Per day: session usage per model family and worker dollars."""
        sessions = self._conn.execute(
            "SELECT substr(ts, 1, 10) AS day, model, sum(input_tokens) AS input_tokens, "
            "sum(output_tokens) AS output_tokens, sum(cache_read) AS cache_read, sum(cache_write) AS cache_write "
            "FROM usage WHERE ts >= ? GROUP BY day, model ORDER BY day", (_days_ago(days),)).fetchall()
        workers = self._conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, round(sum(cost_usd), 4) AS usd, count(*) AS attempts "
            "FROM attempts WHERE created_at >= ? GROUP BY day ORDER BY day", (_days_ago(days),)).fetchall()
        return [{"kind": "session", **dict(r)} for r in sessions] + [{"kind": "worker", **dict(r)} for r in workers]

    def routing_stats(self) -> list[dict]:
        """Per kind of task: how many ran, where they started and passed, how often
        they had to climb, and what they cost. The evidence for changing a ladder."""
        rows = self._conn.execute(
            "SELECT t.kind, t.start_cell, t.final_cell, t.status, t.cost_usd, t.pinned, "
            "(SELECT count(*) FROM attempts a WHERE a.task_id = t.id) AS attempts "
            "FROM tasks t WHERE t.source = 'cauce' AND t.kind IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    def moves(self) -> list[dict]:
        """Every move a run made after a failed attempt, with the task's kind, the
        cell it left, the failure that moved it, and the cell the next attempt ran
        at and whether that one passed: how each ladder is really climbed."""
        rows = self._conn.execute(
            "SELECT t.kind, a.cell, a.failure, a.move, b.cell AS next_cell, b.passed AS next_passed "
            "FROM attempts a JOIN tasks t ON t.id = a.task_id "
            "LEFT JOIN attempts b ON b.task_id = a.task_id AND b.seq = a.seq + 1 "
            "WHERE a.move IS NOT NULL AND t.source = 'cauce' AND t.kind IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    def events_of(self, task_id: int, kind: str) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, kind))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def last_event(self, task_id: int, kind: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def last_event_id(self) -> int:
        return int(self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def expected_cost(self, kind: str, *, repo: str | None = None, limit: int = 20) -> tuple[float, int] | None:
        """The average cost of the last finished tasks of a kind, and how many."""
        scope, params = ("AND repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT cost_usd FROM tasks WHERE kind = ? AND source = 'cauce' "  # noqa: S608
            f"AND status = 'done' {scope} ORDER BY id DESC LIMIT ?", [kind, *params, limit]).fetchall()
        if not rows:
            return None
        return sum(r[0] for r in rows) / len(rows), len(rows)

    # --- habits ------------------------------------------------------------------

    def add_tool_event(self, **record: Any) -> None:
        record = {"ts": now(), **record}
        cols = ", ".join(record)
        self._conn.execute(f"INSERT INTO tool_events ({cols}) VALUES ({', '.join('?' for _ in record)})",  # noqa: S608
                           list(record.values()))

    def tool_events(self, *, days: int = 30, limit: int = 50_000) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tool_events WHERE ts >= ? ORDER BY id LIMIT ?", (_days_ago(days), limit)).fetchall()
        return [dict(r) for r in rows]

    def passed_attempt_events(self, kind: str, *, repo: str | None = None, limit: int = 20_000) -> list[dict]:
        """Tool events recorded inside worker attempts that passed, for one kind."""
        scope, params = ("AND t.repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            "SELECT e.* FROM tool_events e JOIN tasks t ON t.id = e.task_id "  # noqa: S608
            "JOIN attempts a ON a.task_id = e.task_id AND a.seq = e.attempt AND a.passed = 1 "
            f"WHERE t.kind = ? {scope} ORDER BY e.id LIMIT ?", [kind, *params, limit]).fetchall()
        return [dict(r) for r in rows]

    def add_habit(self, *, steps: list[str], command: str, trigger_suffix: str, settings_path: str,
                  candidate: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO habits (candidate, steps, command, trigger_suffix, settings_path, installed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (candidate, json.dumps(steps), command, trigger_suffix, settings_path, now()))
        return int(cur.lastrowid)

    def habit(self, habit_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        return dict(row) if row else None

    def habits(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM habits WHERE removed = 0 ORDER BY id")]

    def update_habit(self, habit_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE habits SET {sets} WHERE id = ?", [*fields.values(), habit_id])  # noqa: S608

    def habit_ran(self, habit_id: int, *, ok: bool, max_failures: int) -> None:
        """Count a run; reset the streak on success, turn the habit off on the third failure."""
        if ok:
            self._conn.execute("UPDATE habits SET runs = runs + 1, failures = 0 WHERE id = ?", (habit_id,))
            return
        self._conn.execute(
            "UPDATE habits SET runs = runs + 1, failures = failures + 1, "
            "disabled_at = CASE WHEN failures + 1 >= ? THEN ? ELSE disabled_at END WHERE id = ?",
            (max_failures, now(), habit_id))

    # --- events: what a watcher reads ---------------------------------------

    def add_event(self, task_id: int, event: str, /, **data: Any) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (task_id, ts, kind, data) VALUES (?, ?, ?, ?)",
            (task_id, now(), event, json.dumps(data, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid)

    def events(self, *, after: int = 0, task_id: int | None = None, limit: int = 200) -> list[dict]:
        """Events after `after`, oldest first: a watcher keeps the last id it saw
        and asks for what came since."""
        scope, params = ("AND task_id = ?", [task_id]) if task_id is not None else ("", [])
        rows = self._conn.execute(
            f"SELECT * FROM events WHERE id > ? {scope} ORDER BY id LIMIT ?",  # noqa: S608
            [after, *params, limit],
        ).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def request_cancel(self, task_id: int, via: str = "cli") -> dict | None:
        """Flag a run to stop, and say where the request came from: the run that
        stops reads it back, so its account can say a person cancelled it, and how."""
        self._conn.execute("UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now(), task_id))
        self.add_event(task_id, "cancel_requested", via=via)
        return self.get_task(task_id)

    def denials(self, task_id: int) -> dict[int, list[str]]:
        """What the permission settings refused, per attempt `seq`: kept on the
        attempt's event, not in the attempts table."""
        rows = self._conn.execute(
            "SELECT data FROM events WHERE task_id = ? AND kind = 'attempt_finished' ORDER BY id", (task_id,))
        out: dict[int, list[str]] = {}
        for row in rows:
            data = json.loads(row[0])
            if data.get("seq") is not None and data.get("denied"):
                out[int(data["seq"])] = list(data["denied"])
        return out

    def cancel_requested(self, task_id: int) -> bool:
        row = self._conn.execute("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return bool(row and row[0])

    # --- problems and fixes: memory across repositories -------------------

    def open_problem(self, title: str, *, repo: str | None, symptom: str = "") -> int:
        """The problem with this title in this repository, opening it if new.

        A problem that was solved and turns up again is *recurring*: the fix
        that solved it stopped holding, and is marked as disproved now.
        """
        stamp = now()
        row = self._conn.execute(
            "SELECT id, state FROM problems WHERE title = ? AND repo IS ? ORDER BY id DESC LIMIT 1",
            (title, repo),
        ).fetchone()
        if row:
            problem_id, state = int(row[0]), row[1]
            if state == "solved":
                self._conn.execute(
                    "UPDATE problems SET state = 'recurring', last_seen = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, problem_id))
                self._conn.execute(
                    "UPDATE fixes SET invalidated_on = ? WHERE problem_id = ? AND outcome = 'worked' "
                    "AND invalidated_on IS NULL", (stamp, problem_id))
            else:
                self._conn.execute("UPDATE problems SET last_seen = ? WHERE id = ?", (stamp, problem_id))
            return problem_id
        cur = self._conn.execute(
            "INSERT INTO problems (repo, title, symptom, state, created_at, updated_at, first_seen, last_seen) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (repo, title, symptom, stamp, stamp, stamp, stamp),
        )
        problem_id = int(cur.lastrowid)
        self._index("problem", problem_id, repo, f"{title}\n{symptom}")
        return problem_id

    def add_fix(
        self,
        problem_id: int,
        description: str,
        outcome: str,
        *,
        repo: str | None,
        why: str = "",
        evidence: str = "",
        task_id: int | None = None,
        commit_sha: str | None = None,
    ) -> int:
        if outcome not in FIX_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        stamp = now()
        cur = self._conn.execute(
            "INSERT INTO fixes (problem_id, repo, description, outcome, why, evidence, task_id, created_at, "
            "believed_from, commit_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (problem_id, repo, description, outcome, why, evidence[:4000], task_id, stamp,
             stamp if outcome == "worked" else None, commit_sha),
        )
        fix_id = int(cur.lastrowid)
        self._index("fix", fix_id, repo, f"{description}\n{why}")
        if outcome == "worked":
            self._conn.execute(
                "UPDATE problems SET state = 'solved', updated_at = ?, last_seen = ? WHERE id = ?",
                (stamp, stamp, problem_id))
        return fix_id

    def invalidate_fix(self, fix_id: int, why: str) -> dict | None:
        """A fix that was believed to work, shown wrong: its problem is open again."""
        row = self._conn.execute("SELECT problem_id FROM fixes WHERE id = ?", (fix_id,)).fetchone()
        if row is None:
            return None
        stamp = now()
        self._conn.execute(
            "UPDATE fixes SET invalidated_on = ?, why = CASE WHEN why = '' THEN ? ELSE why || '; ' || ? END "
            "WHERE id = ?", (stamp, why, why, fix_id))
        self._conn.execute(
            "UPDATE problems SET state = 'recurring', updated_at = ? WHERE id = ? AND state = 'solved'",
            (stamp, row[0]))
        self._index("fix", fix_id, None, why)
        return self.problem(int(row[0]))

    def set_fix_commit(self, task_id: int, commit_sha: str) -> None:
        self._conn.execute(
            "UPDATE fixes SET commit_sha = ? WHERE task_id = ? AND outcome = 'worked' AND commit_sha IS NULL",
            (commit_sha, task_id))

    def problem(self, problem_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM problems WHERE id = ?", (problem_id,)).fetchone()
        if not row:
            return None
        fixes = self._conn.execute(
            "SELECT * FROM fixes WHERE problem_id = ? ORDER BY id", (problem_id,)
        ).fetchall()
        return {**dict(row), "fixes": [dict(f) for f in fixes]}

    def search(self, query: str, *, repo: str | None = None, limit: int = 10, strict: bool = False) -> list[dict]:
        """Problems matching `query` in any repository, this repository's first.

        Every word must match; when nothing does, any word may. `strict` keeps
        only problems sharing several words with the query: a hook putting text
        in front of the model wants no loose matches, and a whole prompt rarely
        matches word for word. The repository
        only orders the results — scoping it out would hide the dead end that
        was found next door.
        """
        terms = [w.replace('"', "") for w in words(query)][:12]
        if not terms:
            return []
        hits: list[sqlite3.Row] = []
        for joiner in (" ", " OR "):
            match = joiner.join(f'"{t}"' for t in terms)
            hits = self._conn.execute(
                "SELECT kind, ref_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT 100",
                (match,),
            ).fetchall()
            if hits:
                break
        problem_ids: list[int] = []
        for hit in hits:
            pid = int(hit["ref_id"]) if hit["kind"] == "problem" else self._problem_of_fix(int(hit["ref_id"]))
            if pid is not None and pid not in problem_ids:
                problem_ids.append(pid)
        found = [p for p in (self.problem(pid) for pid in problem_ids) if p]
        if strict:
            wanted = set(terms)
            found = [p for p in found if _overlap(wanted, p) >= min(STRICT_OVERLAP, len(wanted))]
        found.sort(key=lambda p: 0 if repo is not None and p["repo"] == repo else 1)
        return found[:limit]

    def dead_ends(
        self, query: str | None = None, *, repo: str | None = None, limit: int = 8, strict: bool = False
    ) -> list[dict]:
        """Fixes that failed — and fixes that worked until they did not — each
        with what worked instead when something did."""
        problems = (self.search(query, repo=repo, limit=50, strict=strict) if query
                    else self._recent_problems(repo, 50))
        out: list[dict] = []
        for problem in problems:
            holding = [f for f in problem["fixes"] if f["outcome"] == "worked" and not f["invalidated_on"]]
            for fix in problem["fixes"]:
                disproved = fix["outcome"] == "worked" and fix["invalidated_on"]
                if fix["outcome"] != "failed" and not disproved:
                    continue
                out.append({
                    "problem_id": problem["id"],
                    "problem": problem["title"],
                    "repo": problem["repo"],
                    "tried": fix["description"],
                    "why": fix["why"] or ("believed to work, then the problem came back" if disproved else ""),
                    "worked_instead": holding[-1]["description"] if holding else "",
                    "disproved_on": fix["invalidated_on"] if disproved else None,
                })
                if len(out) >= limit:
                    return out
        return out

    def recent_problems(self, *, repos: Iterable[str] | None = None, limit: int = 40) -> list[dict]:
        """Problems most recently touched, with their fixes; with `repos`, only theirs."""
        scope, params = _in("repo", repos)
        if scope is None:
            return []
        rows = self._conn.execute(
            f"SELECT id FROM problems {scope} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        ).fetchall()
        return [p for p in (self.problem(int(r[0])) for r in rows) if p]

    def _recent_problems(self, repo: str | None, limit: int) -> list[dict]:
        scope, params = ("WHERE repo = ?", [repo]) if repo is not None else ("", [])
        rows = self._conn.execute(
            f"SELECT id FROM problems {scope} ORDER BY updated_at DESC, id DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        ).fetchall()
        return [p for p in (self.problem(int(r[0])) for r in rows) if p]

    def _problem_of_fix(self, fix_id: int) -> int | None:
        row = self._conn.execute("SELECT problem_id FROM fixes WHERE id = ?", (fix_id,)).fetchone()
        return int(row[0]) if row else None

    def _index(self, kind: str, ref_id: int, repo: str | None, text: str) -> None:
        self._conn.execute(
            "INSERT INTO memory_fts (kind, ref_id, repo, text) VALUES (?, ?, ?, ?)",
            (kind, ref_id, repo, fold(text)),
        )


def _overlap(wanted: set[str], problem: dict) -> int:
    text = " ".join([problem["title"], problem["symptom"], *(f["description"] for f in problem["fixes"])])
    return len(wanted & set(words(text)))
