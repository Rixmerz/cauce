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

from cauce import flow, habits, project, stops, usage
from cauce.matrix import LADDERS
from cauce.store import Store

TASK_SOURCES = ("cauce", "queue")  # what the board shows: work cauce runs or holds
#: States that are waiting on a person, and what each one asks of them.
NEEDS_YOU = {
    "failed": "every cell on its ladder failed: read the attempts",
    "blocked": "a refused command, the environment or the budget stopped it: clear it, then `cauce resume`",
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
    attempts = store.attempts(task_id)
    return {"kind": planned["data"].get("kind"), "start": planned["data"].get("start"),
            "ladder": planned["data"].get("ladder") or [], "reasons": planned["data"].get("reasons") or [],
            "steps": [{**{k: a.get(k) for k in keys}, "climb": c}
                      for a, c in zip(attempts, climbs_of(store, task_id, attempts), strict=True)]}


#: Moves after which another attempt ran, and the dial each one turned.
_AXIS = {"retry": "retry", "more_effort": "effort", "next_model": "model", "more_turns": "turns"}


def climbs_of(store: Store, task_id: int, attempts: list[dict]) -> list[dict[str, Any] | None]:
    """For each attempt, the move that followed it: from which cell to which, along
    which dial, and the evidence. A run from before the moves kept their evidence
    gets what its attempts say (the destination is the next attempt's cell)."""
    recorded = {e["data"]["seq"]: e["data"] for e in store.events_of(task_id, "moved") if "seq" in e["data"]}
    out: list[dict[str, Any] | None] = []
    for i, a in enumerate(attempts):
        if not a.get("move"):
            out.append(None)
            continue
        found = recorded.get(a["seq"])
        if found is not None:
            out.append({"from": found.get("from_cell"), "to": found.get("to_cell"), "axis": found.get("axis"),
                        "trigger": found.get("trigger"), "because": found.get("because") or [],
                        "skipped": found.get("skipped") or [], "turns_from": found.get("turns_from"),
                        "turns_to": found.get("turns_to"), "budget_left": found.get("budget_left")})
            continue
        following = attempts[i + 1] if i + 1 < len(attempts) and a["move"] in _AXIS else None
        out.append({"from": a["cell"], "to": following["cell"] if following else None,
                    "axis": _AXIS.get(a["move"], "stop"), "trigger": a.get("failure"),
                    "because": [a["move_reason"]] if a.get("move_reason") else [], "skipped": [],
                    "recovered": True})
    return out


def worker_of(store: Store, task: dict[str, Any], steps: int) -> dict[str, Any] | None:
    """The one-shot worker a running task has out now: a `claude -p` that cauce launched for this
    attempt, with the cell, limits and capabilities it was given. None between attempts."""
    started = store.last_event(task["id"], "attempt_started")
    if started is None or started["data"].get("seq", 0) <= steps:
        return None
    d = started["data"]
    return {"seq": d.get("seq"), "cell": d.get("cell"), "model": d.get("model"), "max_turns": d.get("max_turns"),
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
            # The generic ask is the fallback; the account says what stopped this one, who did, and the next step.
            needs.append({**item, "asks": NEEDS_YOU[t["status"]], "stop": stops.view(store, t)})
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
            if branch and t["updated_at"] >= _days_ago_iso(REVIEW_DAYS) and not _landed(t, branch, finished):
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


def _landed(task: dict, branch: str, finished: dict | None) -> bool:
    """A branch merged (or squash-merged, or deleted) waits on nobody. A read of
    git, never a write; unknown, it is still asked for."""
    from cauce import isolate

    where = Path(task["cwd"]) if task.get("cwd") else None
    if where is None or not where.is_dir():
        return False
    try:
        changed = (finished or {}).get("data", {}).get("changed") or []
        return isolate.landed(where, branch, changed)
    except OSError:
        return False


def task_detail(store: Store, task_id: int) -> dict[str, Any] | None:
    task = store.get_task(task_id)
    if task is None:
        return None
    attempts = store.attempts(task_id)
    refused = store.denials(task_id)
    for a, climb in zip(attempts, climbs_of(store, task_id, attempts), strict=True):
        a["changed_paths"] = json.loads(a["changed_paths"] or "[]")
        a["capabilities"] = json.loads(a["capabilities"] or "[]")
        a["denied"] = refused.get(a["seq"], [])
        a["climb"] = climb
    finished = store.last_event(task_id, "finished")
    return {
        "task": {**_brief(task), "body": task["body"], "options": json.loads(task.get("options") or "{}"),
                 "class_source": task["class_source"], "class_reason": task["class_reason"]},
        "plan": (store.last_event(task_id, "planned") or {}).get("data"),
        "attempts": attempts,
        "messages": [{**m, "author": author(m["role"], task)} for m in store.messages(task_id)],
        "children": [_brief(c) for c in store.children(task_id)],
        "stop": stops.view(store, task),
        "branch": finished["data"].get("branch") if finished else None,
        "impact": finished["data"].get("impact", []) if finished else [],
        # What it was shown when it ran; the memory as it is now would include later ones.
        "dead_ends": ((store.last_event(task_id, "dead_ends") or {}).get("data") or {}).get("shown") or [],
        "events": store.events(task_id=task_id, limit=500),
    }


#: Who wrote a message, in words.
AUTHORS = {"person": "you", "orchestrator": "main session (orchestrator)", "assistant": "main session's answer",
           "worker": "cauce report"}


def author(role: str, task: dict[str, Any]) -> str:
    """Who wrote a message. A session's own prompt is the person's. Work recorded
    before authors were kept says `user` whoever wrote it; sent from a session,
    it was almost always the main session, and the label says it is inferred."""
    if role in AUTHORS:
        return AUTHORS[role]
    if role == "user":
        if task.get("source") == "hook" or not task.get("session_id"):
            return "you"
        return "main session (orchestrator, inferred: recorded before cauce kept who wrote it)"
    return role


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
    moves: dict[str, Counter] = defaultdict(Counter)
    landed: dict[str, Counter] = defaultdict(Counter)
    for m in store.moves():
        to = m["next_cell"] if m["move"] in _AXIS else None
        key = (m["cell"], m["move"], to, m["failure"] or "inconclusive")
        moves[m["kind"]][key] += 1
        if to and m["next_passed"]:
            landed[m["kind"]][key] += 1
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
            # How this ladder is really climbed: each move with the failure behind it, how often, and how
            # often the next attempt passed. The evidence for moving a start or a rung.
            "climbs": [{"from": f, "move": mv, "to": to, "after": why, "times": n,
                        "then_passed": landed[name][(f, mv, to, why)] if to else None}
                       for (f, mv, to, why), n in moves[name].most_common()],
        })
    return out


def memory(store: Store, query: str = "", limit: int = 40, repos: set[str] | None = None) -> list[dict[str, Any]]:
    """Problems and every fix tried on them; with `repos`, only those repositories'."""
    if query.strip():
        found = store.search(query, limit=200 if repos is not None else limit)
        return [p for p in found if repos is None or p["repo"] in repos][:limit]
    return store.recent_problems(repos=repos, limit=limit)


def notes_view(store: Store, repo_key: str, *, topic: str = "", query: str = "", state: str = "") -> dict[str, Any]:
    """One project's notes: the topics with their counts, and the notes asked for,
    each with its anchors and links. Read only: a GET never reviews."""
    from cauce import notes

    counts = store.note_counts(repo_key)
    topics = [{"name": name, "description": about,
               "live": sum(counts.get(name, {}).get(s, 0) for s in notes.LIVE),
               "review": counts.get(name, {}).get("review", 0)}
              for name, about in notes.topics(store, repo_key).items()]
    states = [state] if state in notes.STATES else list(notes.LIVE)
    in_topics = [topic] if topic else None
    if query.strip():
        found = notes.recall(store, repo_key, query, in_topics=in_topics, limit=40, hops=1, states=states)
    else:
        rows = store.notes(repo_key, topics=in_topics, states=states, limit=200)
        found = notes.expand(store, [r["id"] for r in rows])
    titles = {n["id"]: n["title"] for n in found}
    for n in found:
        for x in n["links"]:
            if x["to"] not in titles:
                other = store.get_note(x["to"])
                titles[x["to"]] = other["title"] if other else ""
            x["title"] = titles[x["to"]]
    proposals = [{"id": n["id"], "title": n["title"], "topic": n["proposed_topic"]}
                 for n in store.notes(repo_key, states=notes.LIVE) if n["proposed_topic"]]
    return {"topics": topics, "notes": found, "proposals": proposals}


def projects(store: Store, enrolled_only: bool = True) -> list[dict[str, Any]]:
    """Every repository cauce has worked in, at the top of the checkout it was last used from.
    Only enrolled ones (a `.cauce/` folder) unless asked: the plugin being on is not using cauce."""
    from cauce import repo as repo_mod

    out = []
    for p in store.projects():
        where = Path(p["cwd"]) if p["cwd"] else None
        top = repo_mod.toplevel(where) if where is not None and where.is_dir() else None
        enrolled = project.find(where) is not None if where is not None else False
        if enrolled_only and not enrolled:
            continue
        out.append({**p, "dir": str(top or where or ""), "exists": bool(where and where.is_dir()),
                    "enrolled": enrolled})
    return out


def sessions(store: Store, repos: set[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Claude Code sessions in enrolled projects — those with a `.cauce/` folder in the session's
    directory or the checkout above it — with their name and the command that resumes each."""
    import shlex

    out, names = [], {}
    for s in store.sessions(repos=repos, limit=limit * 5):
        folder = project.find(s["cwd"])
        if folder is None:
            continue
        if folder not in names:
            names[folder] = project.names(folder)
        entry = names[folder].get(s["id"])
        name = str((entry or {}).get("name") or "").strip() or None
        out.append({**s, "resume": f"cd {shlex.quote(s['cwd'] or '.')} && claude --resume {s['id']}",
                    "name": name, "named_by": ("you" if project.person_named(entry) else "haiku") if name else None})
        if len(out) >= limit:
            break
    return out


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
