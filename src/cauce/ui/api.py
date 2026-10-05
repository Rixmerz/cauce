"""What each screen shows, as plain data. No HTTP here: every screen is testable
against a store, and the server only turns these into JSON."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from cauce import habits, usage
from cauce.matrix import LADDERS
from cauce.store import Store

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


def _brief(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "status", "kind", "repo", "cwd", "start_cell", "final_cell", "current_cell",
            "cost_usd", "source", "created_at", "updated_at", "result", "pinned")
    return {k: task.get(k) for k in keys}


def board(store: Store) -> dict[str, Any]:
    tasks = store.list_tasks(limit=500)
    needs, running, done = [], [], []
    for t in tasks:
        if t["source"] == "delegation":
            continue
        item = _brief(t)
        if t["status"] in NEEDS_YOU and t["source"] in ("cauce", "queue"):
            needs.append({**item, "asks": NEEDS_YOU[t["status"]]})
        elif t["status"] == "running":
            started = store.last_event(t["id"], "attempt_started")
            item["attempt"] = started["data"].get("seq") if started else None
            running.append(item)
        elif t["status"] == "done":
            finished = store.last_event(t["id"], "finished")
            branch = finished["data"].get("branch") if finished else None
            item["branch"] = branch
            done.append(item)
            if branch and t["updated_at"] >= _days_ago_iso(REVIEW_DAYS):
                needs.append({**item, "asks": f"review branch {branch}, then merge it"})
    lanes = {lane["repo"]: lane for lane in store.lanes()}
    queued: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": []})
    for t in reversed(store.list_tasks(status=["queued"], limit=500)):
        lane = queued[t["repo"] or ""]
        lane["repo"] = t["repo"] or ""
        lane["paused"] = bool(lanes.get(t["repo"] or "", {}).get("paused"))
        lane["reason"] = lanes.get(t["repo"] or "", {}).get("reason")
        lane["tasks"].append(_brief(t))
    for repo, lane in lanes.items():
        if lane["paused"] and repo not in queued:
            queued[repo] = {"repo": repo, "paused": True, "reason": lane["reason"], "tasks": []}
    return {
        "counts": {"needs_you": len(needs), "running": len(running),
                   "queued": sum(len(v["tasks"]) for v in queued.values()), "done": len(done)},
        "needs_you": needs,
        "running": running,
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


def memory(store: Store, query: str = "", limit: int = 40) -> list[dict[str, Any]]:
    if query.strip():
        return store.search(query, limit=limit)
    return store._recent_problems(None, limit)


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
