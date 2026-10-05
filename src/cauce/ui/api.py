"""What each screen shows, as plain data. No HTTP here: every screen is testable
against a store, and the server only turns these into JSON."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cauce import flow, habits, usage
from cauce.matrix import LADDERS
from cauce.store import Store

TASK_SOURCES = ("cauce", "queue")  # what the board shows: work cauce runs or holds
#: States that are waiting on a person, and what each one asks of them.
NEEDS_YOU = {
    "failed": "every cell on its ladder failed: read the attempts",
    "blocked": "the environment or the budget stopped it",
    "replan": "the task as written cannot be done: rewrite or split it",
    "needs_approval": "the next cell needs your approval",
    "interrupted": "it stopped with no sign of life",
}
#: How far back a passed task's branch still counts as waiting for a review.
REVIEW_DAYS = 3
#: How much of a task's text the board carries; the task view has all of it.
BODY_CHARS = 800


def _brief(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "status", "kind", "repo", "cwd", "start_cell", "final_cell", "current_cell",
            "cost_usd", "source", "created_at", "updated_at", "result", "pinned", "session_id",
            "parallel", "parallel_reason")
    body = task.get("body") or ""
    return {**{k: task.get(k) for k in keys},
            "body": body if len(body) <= BODY_CHARS else body[:BODY_CHARS - 1] + "…"}


def flow_of(store: Store, task_id: int) -> dict[str, Any] | None:
    """A task's way through the matrix: its kind, its ladder and where it started, then every attempt
    with the cell it ran in, how it ended and the move that followed. None for a task cauce never planned
    (a prompt answered in a session)."""
    planned = store.last_event(task_id, "planned")
    if planned is None:
        return None
    keys = ("seq", "cell", "passed", "failure", "move", "move_reason", "cost_usd", "turns", "duration_s")
    return {"kind": planned["data"].get("kind"), "start": planned["data"].get("start"),
            "ladder": planned["data"].get("ladder") or [], "reasons": planned["data"].get("reasons") or [],
            "steps": [{k: a.get(k) for k in keys} for a in store.attempts(task_id)]}


def worker_of(store: Store, task: dict[str, Any], steps: int) -> dict[str, Any] | None:
    """The one-shot worker a running task has out now: a `claude -p` that cauce launched for this
    attempt, with the cell, limits and capabilities it was given. None between attempts."""
    started = store.last_event(task["id"], "attempt_started")
    if started is None or started["data"].get("seq", 0) <= steps:
        return None
    d = started["data"]
    return {"seq": d.get("seq"), "cell": d.get("cell"), "max_turns": d.get("max_turns"),
            "budget_usd": d.get("budget_usd"), "capabilities": d.get("capabilities") or [],
            "started_at": started["ts"], "alive": flow.alive(task.get("pid"))}


def board(store: Store, repos: set[str] | None = None) -> dict[str, Any]:
    """Every lane's state; with `repos`, only those repositories' tasks and lanes."""
    tasks = store.list_tasks(limit=500, source=TASK_SOURCES)
    needs, running, done = [], [], []
    for t in tasks:
        if repos is not None and t["repo"] not in repos:
            continue
        item = _brief(t)
        if t["status"] in NEEDS_YOU or t["status"] in ("running", "done"):
            item["flow"] = flow_of(store, t["id"])
        if t["status"] in NEEDS_YOU:
            needs.append({**item, "asks": NEEDS_YOU[t["status"]]})
        elif t["status"] == "running":
            started = store.last_event(t["id"], "attempt_started")
            item["attempt"] = started["data"].get("seq") if started else None
            steps = len((item.get("flow") or {}).get("steps", []))
            item["worker"] = worker_of(store, t, steps) if item.get("flow") else None
            running.append(item)
        elif t["status"] == "done":
            finished = store.last_event(t["id"], "finished")
            branch = finished["data"].get("branch") if finished else None
            item["branch"] = branch
            done.append(item)
            if branch and t["updated_at"] >= _days_ago_iso(REVIEW_DAYS):
                needs.append({**item, "asks": f"review branch {branch}, then merge it"})
    # A prompt answered in a session is recorded as a task too (it carries the
    # session's follow-ups and result), but it is not work on the board: every
    # turn would be a card. The sessions answering right now are listed apart.
    answering = [_brief(t) for t in store.list_tasks(status=["running"], limit=100, source=("hook",))
                 if repos is None or t["repo"] in repos]
    lanes = {lane["repo"]: lane for lane in store.lanes()}
    queued: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": []})
    for t in reversed(store.list_tasks(status=["queued"], limit=500)):
        if repos is not None and t["repo"] not in repos:
            continue
        lane = queued[t["repo"] or ""]
        lane["repo"] = t["repo"] or ""
        lane["paused"] = bool(lanes.get(t["repo"] or "", {}).get("paused"))
        lane["reason"] = lanes.get(t["repo"] or "", {}).get("reason")
        lane["tasks"].append(_brief(t))
    for repo, lane in lanes.items():
        if lane["paused"] and repo not in queued and (repos is None or repo in repos):
            queued[repo] = {"repo": repo, "paused": True, "reason": lane["reason"], "tasks": []}
    from cauce import dispatch

    for lane in queued.values():
        lane["dispatching"] = dispatch.held(store.path.parent, lane["repo"] or "*")
    return {
        "counts": {"needs_you": len(needs), "running": len(running),
                   "workers": sum(1 for r in running if r.get("worker")),
                   "queued": sum(len(v["tasks"]) for v in queued.values()), "done": len(done),
                   "answering": len(answering)},
        "needs_you": needs,
        "running": running,
        "answering": answering,
        "queued": sorted(queued.values(), key=lambda lane: lane["repo"]),
        "done": done[:60],
        "last_event": store.last_event_id(),
    }


def task_detail(store: Store, task_id: int) -> dict[str, Any] | None:
    task = store.get_task(task_id)
    if task is None:
        return None
    attempts = store.attempts(task_id)
    for a in attempts:
        a["changed_paths"] = json.loads(a["changed_paths"] or "[]")
        a["capabilities"] = json.loads(a["capabilities"] or "[]")
    finished = store.last_event(task_id, "finished")
    return {
        "task": {**_brief(task), "body": task["body"], "options": json.loads(task.get("options") or "{}"),
                 "class_source": task["class_source"], "class_reason": task["class_reason"]},
        "plan": (store.last_event(task_id, "planned") or {}).get("data"),
        "attempts": attempts,
        "messages": store.messages(task_id),
        "children": [_brief(c) for c in store.children(task_id)],
        "branch": finished["data"].get("branch") if finished else None,
        "impact": finished["data"].get("impact", []) if finished else [],
        "dead_ends": store.dead_ends(task["body"], repo=task["repo"], limit=5),
        "events": store.events(task_id=task_id, limit=500),
    }


def spend(store: Store, days: int = 7) -> dict[str, Any]:
    report = usage.spend(store, days=days)
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in store.spend_by_day(days=days):
        if row["kind"] == "session":
            fam = usage.family(row["model"])
            by_day[row["day"]][fam] += usage.dollars(row["model"], row["input_tokens"], row["output_tokens"],
                                                     row["cache_read"], row["cache_write"])
        else:
            by_day[row["day"]]["workers"] += row["usd"] or 0
    report["by_day"] = [{"day": d, **{k: round(v, 4) for k, v in parts.items()}} for d, parts in sorted(by_day.items())]
    return report


def routing(store: Store) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for row in store.routing_stats():
        kind = stats.setdefault(row["kind"], {"kind": row["kind"], "tasks": 0, "passed": 0, "climbed": 0,
                                              "pinned": 0, "cost_usd": 0.0, "starts": Counter(), "passes": Counter()})
        kind["tasks"] += 1
        kind["cost_usd"] += row["cost_usd"] or 0
        kind["pinned"] += row["pinned"]
        kind["starts"][row["start_cell"]] += 1
        if row["status"] == "done":
            kind["passed"] += 1
            kind["passes"][row["final_cell"]] += 1
        if row["attempts"] > 1:
            kind["climbed"] += 1
    out = []
    for name, ladder in LADDERS.items():
        k = stats.get(name, {"kind": name, "tasks": 0, "passed": 0, "climbed": 0, "pinned": 0, "cost_usd": 0.0,
                             "starts": Counter(), "passes": Counter()})
        out.append({
            **{key: k[key] for key in ("kind", "tasks", "passed", "climbed", "pinned")},
            "cost_usd": round(k["cost_usd"], 4),
            "ladder": [c.label for c in ladder],
            "starts": dict(k["starts"]),
            "passes": dict(k["passes"]),
        })
    return out


def memory(store: Store, query: str = "", limit: int = 40, repos: set[str] | None = None) -> list[dict[str, Any]]:
    """Problems and every fix tried on them; with `repos`, only those repositories'."""
    if query.strip():
        found = store.search(query, limit=200 if repos is not None else limit)
        return [p for p in found if repos is None or p["repo"] in repos][:limit]
    return store.recent_problems(repos=repos, limit=limit)


def projects(store: Store) -> list[dict[str, Any]]:
    """Every repository cauce has worked in, at the top of the checkout it was last used from."""
    from cauce import repo as repo_mod

    out = []
    for p in store.projects():
        where = Path(p["cwd"]) if p["cwd"] else None
        top = repo_mod.toplevel(where) if where is not None and where.is_dir() else None
        out.append({**p, "dir": str(top or where or ""), "exists": bool(where and where.is_dir())})
    return out


def sessions(store: Store, repos: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Claude Code sessions cauce has seen, with the command that resumes each where it ran."""
    import shlex

    return [{**s, "resume": f"cd {shlex.quote(s['cwd'] or '.')} && claude --resume {s['id']}"}
            for s in store.sessions(repos=repos, limit=limit)]


SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def claude_tasks(session_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The session's own task list, as Claude Code keeps it: one JSON file per task
    under `<config>/tasks/<session>/` (older versions: one todo file under `todos/`).

    Read only, and read defensively: the layout is Claude Code's, not a contract
    cauce owns. `state` says which of absent, read and unreadable it was — "no
    plan" and "could not read the plan" are different answers.
    """
    env = os.environ if env is None else env
    if not SESSION_ID.match(session_id or ""):
        return {"state": "absent", "tasks": []}
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    folder = config_dir / "tasks" / session_id
    try:
        if folder.is_dir():
            tasks = []
            for f in folder.glob("*.json"):
                item = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(item, dict) and item.get("subject"):
                    tasks.append({k: item.get(k) for k in
                                  ("id", "subject", "description", "status", "activeForm", "blockedBy")})
            tasks.sort(key=lambda t: (len(str(t["id"])), str(t["id"])))
            return {"state": "read", "tasks": tasks}
        todos = sorted((config_dir / "todos").glob(f"{session_id}-agent-*.json"))
        if todos:
            items = json.loads(todos[-1].read_text(encoding="utf-8"))
            tasks = [{"id": str(i + 1), "subject": t.get("content"), "description": None, "status": t.get("status"),
                      "activeForm": t.get("activeForm"), "blockedBy": []}
                     for i, t in enumerate(items if isinstance(items, list) else []) if isinstance(t, dict)]
            return {"state": "read", "tasks": tasks}
    except (OSError, ValueError):
        return {"state": "unreadable", "tasks": []}
    return {"state": "absent", "tasks": []}


def _plan_counts(session_id: str) -> dict[str, int] | None:
    plan = claude_tasks(session_id)
    if not plan["tasks"]:
        return None
    return {"done": sum(1 for t in plan["tasks"] if t["status"] == "completed"), "total": len(plan["tasks"])}


def session_list(store: Store, repos: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """`sessions`, each with how far its own task list got."""
    return [{**s, "plan": _plan_counts(s["id"])} for s in sessions(store, repos, limit)]


def session_detail(store: Store, session_id: str, turns: int = 60) -> dict[str, Any] | None:
    """One session: its own task list, the work it handed to cauce, and its recent turns."""
    found = [s for s in sessions(store, limit=100000) if s["id"] == session_id]
    if not found:
        return None
    mine = store.list_tasks(session_id=session_id, limit=500)
    return {
        "session": found[0],
        "plan": claude_tasks(session_id),
        "tasks": [_brief(t) for t in mine if t["source"] in TASK_SOURCES],
        "turns": [{**_brief(t), "result": (t["result"] or "")[:400]} for t in mine if t["source"] == "hook"][:turns],
    }


def habit_view(store: Store, days: int = 30) -> dict[str, Any]:
    habits.load_events(store)
    found = habits.candidates(store.tool_events(days=days))
    return {
        "candidates": [{"id": c.id, "steps": list(c.steps), "occurrences": c.occurrences, "sessions": c.sessions,
                        "success": c.success, "score": c.score} for c in found[:40]],
        "installed": [{**h, "steps": json.loads(h["steps"])} for h in store.habits()],
    }


def _days_ago_iso(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
