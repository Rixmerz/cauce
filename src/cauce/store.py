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

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
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
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  kind UNINDEXED, ref_id UNINDEXED, repo UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);
"""

#: Words a query and a problem must share for a strict match.
STRICT_OVERLAP = 3

TASK_STATES = ("queued", "running", "done", "failed", "interrupted", "blocked", "replan", "needs_approval")
FIX_OUTCOMES = ("worked", "failed", "partial", "pending")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def home(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    if env.get("CAUCE_HOME"):
        return Path(env["CAUCE_HOME"]).expanduser()
    xdg = env.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path.home() / ".local" / "share"
    return base / "cauce"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

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
        limit: int = 50,
    ) -> list[dict]:
        clauses, params = [], []
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
                "SELECT * FROM tasks WHERE session_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
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
            "AND t.final_cell IS NOT NULL AND t.source = 'cauce'" + scope + " ORDER BY t.id DESC LIMIT ?",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    # --- problems and fixes: memory across repositories -------------------

    def open_problem(self, title: str, *, repo: str | None, symptom: str = "") -> int:
        row = self._conn.execute(
            "SELECT id FROM problems WHERE title = ? AND repo IS ? AND state = 'open' ORDER BY id DESC LIMIT 1",
            (title, repo),
        ).fetchone()
        if row:
            return int(row[0])
        stamp = now()
        cur = self._conn.execute(
            "INSERT INTO problems (repo, title, symptom, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'open', ?, ?)",
            (repo, title, symptom, stamp, stamp),
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
    ) -> int:
        if outcome not in FIX_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        cur = self._conn.execute(
            "INSERT INTO fixes (problem_id, repo, description, outcome, why, evidence, task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (problem_id, repo, description, outcome, why, evidence[:4000], task_id, now()),
        )
        fix_id = int(cur.lastrowid)
        self._index("fix", fix_id, repo, f"{description}\n{why}")
        if outcome == "worked":
            self._conn.execute(
                "UPDATE problems SET state = 'solved', updated_at = ? WHERE id = ?", (now(), problem_id)
            )
        return fix_id

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
        """Fixes that failed, each with what worked instead when something did."""
        problems = (self.search(query, repo=repo, limit=50, strict=strict) if query
                    else self._recent_problems(repo, 50))
        out: list[dict] = []
        for problem in problems:
            worked = [f for f in problem["fixes"] if f["outcome"] == "worked"]
            for fix in problem["fixes"]:
                if fix["outcome"] != "failed":
                    continue
                out.append({
                    "problem_id": problem["id"],
                    "problem": problem["title"],
                    "repo": problem["repo"],
                    "tried": fix["description"],
                    "why": fix["why"],
                    "worked_instead": worked[-1]["description"] if worked else "",
                })
                if len(out) >= limit:
                    return out
        return out

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
