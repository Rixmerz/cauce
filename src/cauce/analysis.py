"""Where cauce's time and money go, and what it gets for them: `cauce stats` and
the UI's Analysis screen.

Read from the store only, never by a model. A run is one `cauce run` of a task,
from its `planned` event to its `finished` one: a resumed task has one run per
resume. Each run's phases are on its events (queued, planning, setting up,
working, checking, verifying), and each attempt's cell, turns, tokens and wall
time. Events written before the phases were timed have no such fields: their
runs count by their timestamps, and a phase they never measured is left out of
the breakdown rather than guessed.

What it answers is what the routing needs evidence for: how often a kind passes
first time and how often it climbs, what a pass costs and how long it takes,
where the waiting is, whether the session's thread in a brief changes how a run
goes, and what a session's own turns take beside the work it sends.
"""
from __future__ import annotations

import csv
import io
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from cauce import cells as cells_mod
from cauce import launch, racing, revisions, usage
from cauce.store import Store

#: The phases of a run, in the order they happen. The first four come before its own wall
#: time: the engine queueing it after the step it waited on passed (`advance_s`), its wait
#: for the dispatcher (`pickup_s`) and the process starting (`spawn_s`), which split the
#: older `queued_s` where they were measured.
PHASES = ("advance_s", "pickup_s", "spawn_s", "queued_s", "plan_s", "setup_s", "worker_s", "check_s", "verifier_s",
          "wait_s", "notes_s", "lost_s", "unnoticed_s", "other_s")
#: The phases before a run's own start: never part of its wall time, so never taken from "other".
BEFORE_RUN = frozenset({"advance_s", "pickup_s", "spawn_s", "queued_s"})
#: The events a compaction decision leaves (`cauce.compaction`).
COMPACTION_EVENTS = ("compaction_decided", "compaction_delivered", "compaction_settled", "compaction_seen")
#: How many compaction decisions the text lists, newest last.
COMPACTIONS_SHOWN = 10
#: The columns of a run, as `cauce stats --runs` writes them.
RUN_COLUMNS = ("task", "repo", "kind", "source", "workflow", "step", "status", "stop", "start_cell", "final_cell",
               "started_at", "ended_at", "wall_s", *PHASES[:-1], "attempts", "first_try", "verified",
               "verifier_blocked", "cost_usd", "turns", "input_tokens", "output_tokens", "session_context",
               "session_chars", "brief_chars", "title")


def _when(stamp: str | None) -> datetime | None:
    """A stored time; one written without a zone is UTC, as cauce writes them."""
    try:
        found = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _between(start: str | None, end: str | None) -> float | None:
    a, b = _when(start), _when(end)
    return round((b - a).total_seconds(), 1) if a and b and b >= a else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def pct(values: Iterable[float | None], q: float) -> float | None:
    """The q-th percentile (0..1) by nearest rank; None with no values."""
    found = sorted(v for v in values if v is not None)
    if not found:
        return None
    return round(found[min(len(found) - 1, max(0, math.ceil(q * len(found)) - 1))], 2)


def runs(tasks: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each run of each task, with its phases, attempts, cost and outcome."""
    by_task: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        by_task[int(event["task_id"])].append(event)
    out = []
    for task in tasks:
        current: dict[str, Any] | None = None
        for event in by_task.get(int(task["id"]), ()):
            kind, data = event["kind"], event["data"] or {}
            if kind == "planned":
                current = _start(task, event)
                continue
            if current is None:
                continue
            if kind == "briefed":
                session = data.get("session")
                current["session_context"] = bool(session)
                current["session_chars"] = (session or {}).get("chars", 0)
            elif kind == "attempt_started":
                # An attempt with no `attempt_finished` before the run's end lost its worker (it died,
                # or was cut off): the time from its start to the end is lost, not "other".
                current["_open_attempt"] = event["ts"]
                current["_open_turns"] = data.get("max_turns")
                if current["brief_chars"] is None:
                    current["brief_chars"] = data.get("brief_chars")
                    if (since := _number(data.get("since_start_s"))) is not None:
                        current["setup_s"] = round(max(since - (current["plan_s"] or 0), 0), 2)
            elif kind == "attempt_finished":
                current["_open_attempt"] = None
                _attempt(current, data)
            elif kind == "verified":
                current["_check_usd"] += _number(data.get("cost_usd")) or 0
                current["verified"] += 1
                current["verifier_blocked"] += 0 if data.get("passed") else 1
                current["_verifier_s"] += _number(data.get("wall_s")) or _number(data.get("duration_s")) or 0
            elif kind == "moved":
                current["moves"].append(data.get("move"))
            elif kind == "finished":
                out.append(_end(current, event))
                current = None
    return out


def _start(task: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    data = event["data"] or {}
    return {
        "task": task["id"], "repo": task.get("repo"), "title": task.get("title") or "",
        "kind": data.get("kind") or task.get("kind"), "source": task.get("source"),
        "workflow": task.get("workflow"), "step": task.get("workflow_step"),
        "has_session": bool(task.get("session_id")),
        "start_cell": data.get("start"), "final_cell": None, "status": None, "stop": None,
        "started_at": event["ts"], "ended_at": None, "wall_s": None,
        "advance_s": None, "pickup_s": None, "spawn_s": None,
        "queued_s": _number(data.get("queued_s")), "plan_s": _number(data.get("plan_s")), "setup_s": None,
        "worker_s": None, "check_s": None, "verifier_s": None, "wait_s": None, "hold_s": None, "notes_s": None,
        "lost_s": None, "unnoticed_s": None, "_open_attempt": None, "_open_turns": None,
        "attempts": 0, "first_try": None, "verified": 0, "verifier_blocked": 0, "moves": [], "cells": [],
        "cost_usd": 0.0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "refused": 0,
        "cache_read_tokens": 0, "measured_input_tokens": 0, "verifier_cost_usd": 0.0,
        "session_context": None, "session_chars": 0, "brief_chars": None,
        "_worker_s": 0.0, "_check_s": 0.0, "_verifier_s": 0.0, "_timed": False, "_check_usd": 0.0,
    }


def _attempt(run: dict[str, Any], data: Mapping[str, Any]) -> None:
    run["attempts"] += 1
    if run["first_try"] is None:
        run["first_try"] = bool(data.get("passed"))
    run["cells"].append(data.get("cell"))
    run["cost_usd"] += _number(data.get("cost_usd")) or 0
    run["turns"] += int(data.get("turns") or 0)
    run["input_tokens"] += int(data.get("input_tokens") or 0)
    run["output_tokens"] += int(data.get("output_tokens") or 0)
    if "cache_read_tokens" in data:  # recorded since 0.18: older attempts say nothing of the cache
        run["cache_read_tokens"] += int(data.get("cache_read_tokens") or 0)
        run["measured_input_tokens"] += int(data.get("input_tokens") or 0)
    run["verifier_cost_usd"] += _split(data, run["_check_usd"])[1]
    run["_check_usd"] = 0.0
    run["refused"] += bool(data.get("denied"))
    wall = _number(data.get("wall_s"))
    run["_timed"] = run["_timed"] or wall is not None
    run["_worker_s"] += wall if wall is not None else (_number(data.get("duration_s")) or 0)
    run["_check_s"] += _number(data.get("check_s")) or 0


def _end(run: dict[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    data = event["data"] or {}
    run["ended_at"] = event["ts"]
    run["status"] = data.get("status")
    run["stop"] = data["stop"].get("cause") if isinstance(data.get("stop"), dict) else None
    run["final_cell"] = data.get("final_cell")
    run["wall_s"] = _number(data.get("wall_s")) or _between(run["started_at"], run["ended_at"])
    # The run's own account of its cost wins: it holds the classification and the checks.
    run["cost_usd"] = round(_number(data.get("cost_usd")) or run["cost_usd"], 4)
    for phase in ("worker_s", "check_s", "verifier_s", "wait_s", "hold_s", "advance_s", "pickup_s", "spawn_s",
                  "notes_s"):
        run[phase] = _number(data.get(phase))
    if run["_open_attempt"]:
        # A worker runs at most its attempt's time limit: what passed after it, the run
        # waited dead for someone to notice (a sweep at the next session), not work lost.
        gone = _between(run["_open_attempt"], run["ended_at"])
        turns = run["_open_turns"] if isinstance(run["_open_turns"], int) else 0
        limit = float(launch.timeout_for(turns))
        if gone is not None:
            run["lost_s"] = round(min(gone, limit), 2)
            run["unnoticed_s"] = round(max(gone - limit, 0.0), 2)
    # A run from before the phases were timed: what its attempts measured, the CLI's
    # own duration for the work, and nothing for what was never measured.
    if run["worker_s"] is None and run["_worker_s"]:
        run["worker_s"] = round(run["_worker_s"], 2)
    if run["check_s"] is None and run["_timed"]:
        run["check_s"] = round(run["_check_s"], 2)
    if run["verifier_s"] is None and (run["_timed"] or run["_verifier_s"]):
        run["verifier_s"] = round(run["_verifier_s"], 2)
    return {k: v for k, v in run.items() if not k.startswith("_")}


def phases(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Seconds per phase over the runs whose phases were measured, and each one's share."""
    timed = [r for r in found if r["wall_s"] is not None and r["plan_s"] is not None]
    totals = dict.fromkeys(PHASES, 0.0)
    for r in timed:
        known = 0.0
        # A run whose queue time was split into pickup and spawn is not counted twice.
        split = r.get("pickup_s") is not None or r.get("spawn_s") is not None
        for phase in PHASES[:-1]:
            value = 0.0 if phase == "queued_s" and split else r.get(phase) or 0.0
            totals[phase] += value
            known += value if phase not in BEFORE_RUN else 0.0
        totals["other_s"] += max((r["wall_s"] or 0) - known, 0.0)
    whole = sum(totals.values())
    return {"runs": len(timed), "seconds": {k: round(v, 1) for k, v in totals.items()},
            "share": {k: round(v / whole, 3) if whole else 0.0 for k, v in totals.items()}}


def _summary(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    passed = [r for r in found if r["status"] == "done"]
    first = [r for r in found if r["first_try"] is not None]
    cost = sum(r["cost_usd"] for r in found)
    wall = [r["wall_s"] for r in found]
    hours = sum(w for w in wall if w) / 3600
    return {
        "runs": len(found), "tasks": len({r["task"] for r in found}), "passed": len(passed),
        "pass_rate": round(len(passed) / len(found), 3) if found else None,
        "first_try_rate": round(sum(r["first_try"] for r in first) / len(first), 3) if first else None,
        "climbed_rate": round(sum(r["attempts"] > 1 for r in found) / len(found), 3) if found else None,
        "attempts_avg": round(sum(r["attempts"] for r in found) / len(found), 2) if found else None,
        "wall_p50_s": pct(wall, 0.5), "wall_p90_s": pct(wall, 0.9),
        "queued_p50_s": pct((r["queued_s"] for r in found), 0.5),
        "cost_usd": round(cost, 2), "cost_per_pass_usd": round(cost / len(passed), 2) if passed else None,
        "cost_p50_usd": pct((r["cost_usd"] for r in found), 0.5),
        "hours": round(hours, 2), "minutes_per_pass": round(hours * 60 / len(passed), 1) if passed else None,
        "verified_rate": round(sum(r["verified"] > 0 for r in found) / len(found), 3) if found else None,
        "verifier_cost_usd": round(sum(r["verifier_cost_usd"] for r in found), 2),
        "cache_read_share": _share(sum(r["cache_read_tokens"] for r in found),
                                   sum(r["measured_input_tokens"] for r in found)),
        "refused_rate": round(sum(r["refused"] > 0 for r in found) / len(found), 3) if found else None,
    }


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole else None


def _split(data: Mapping[str, Any], checks_usd: float) -> tuple[float, float]:
    """An attempt's own cost and its independent check's. Kept apart since 0.18;
    before, its `cost_usd` held both, and its `verified` events (`checks_usd`, the
    ones since the attempt before) said what the checks cost."""
    if "worker_cost_usd" in data:
        return _number(data["worker_cost_usd"]) or 0, _number(data.get("verifier_cost_usd")) or 0
    total = _number(data.get("cost_usd")) or 0
    return round(max(total - checks_usd, 0), 4), round(min(checks_usd, total), 4)


def _grouped(found: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in found:
        groups[str(r.get(key) or "—")].append(r)
    return sorted(({key: name, **_summary(rows)} for name, rows in groups.items()),
                  key=lambda row: (-row["runs"], row[key]))


def cells(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    """Every attempt by the cell it ran at: how often it passed, its time, turns, its own
    cost (its independent check's apart) and how much of its input the cache served."""
    by_cell: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    checks: dict[int, float] = defaultdict(float)
    for e in events:
        task = int(e["task_id"])
        if task not in task_ids:
            continue
        if e["kind"] == "verified":
            checks[task] += _number(e["data"].get("cost_usd")) or 0
        elif e["kind"] == "attempt_finished":
            own, check = _split(e["data"], checks.pop(task, 0.0))
            by_cell[str(e["data"].get("cell") or "—")].append({**e["data"], "_own": own, "_check": check})
    out = []
    for cell, rows in by_cell.items():
        out.append({
            "cell": cell, "attempts": len(rows), "passed": sum(bool(d.get("passed")) for d in rows),
            "wall_p50_s": pct((_number(d.get("wall_s")) or _number(d.get("duration_s")) for d in rows), 0.5),
            "turns_p50": pct((_number(d.get("turns")) for d in rows), 0.5),
            "cost_p50_usd": pct((d["_own"] for d in rows), 0.5),
            "cost_usd": round(sum(d["_own"] for d in rows), 2),
            "verifier_cost_usd": round(sum(d["_check"] for d in rows), 2),
            "cache_read_share": _share(sum(int(d.get("cache_read_tokens") or 0) for d in rows),
                                       sum(int(d.get("input_tokens") or 0) for d in rows if "cache_read_tokens" in d)),
            "failures": dict(Counter(d.get("failure") for d in rows if not d.get("passed") and d.get("failure"))),
        })
    return sorted(out, key=lambda row: -row["attempts"])


#: The moves whose next attempt may go on in the last one's session.
CONTINUING_MOVES = ("more_effort", "more_turns")


def continuations(events: Sequence[Mapping[str, Any]], task_ids: set[int]) -> list[dict[str, Any]]:
    """The attempts after more effort or more turns, by whether they went on in the attempt
    before's session or started fresh: how often each passed, its turns and its own cost.
    An attempt says it went on (`continued`, or `resumed` on its start, as cauce first kept
    it); one that says neither started fresh, as every one did before sessions were kept."""
    last_move: dict[int, str | None] = {}
    resumed: set[tuple[int, Any]] = set()
    checks: dict[int, float] = defaultdict(float)
    groups: dict[tuple[str, bool], list[tuple[Mapping[str, Any], float]]] = defaultdict(list)
    for e in events:
        task, data = int(e["task_id"]), e["data"] or {}
        if task not in task_ids:
            continue
        if e["kind"] in ("planned", "moved"):
            last_move[task] = data.get("move")  # a new run's first attempt follows no move
        elif e["kind"] == "attempt_started" and data.get("resumed"):
            resumed.add((task, data.get("seq")))
        elif e["kind"] == "verified":
            checks[task] += _number(data.get("cost_usd")) or 0
        elif e["kind"] == "attempt_finished":
            own = _split(data, checks.pop(task, 0.0))[0]
            move = last_move.pop(task, None)
            if move in CONTINUING_MOVES:
                went_on = data["continued"] if "continued" in data else (task, data.get("seq")) in resumed
                groups[(str(move), bool(went_on))].append((data, own))
    return [{"move": move, "continued": went_on, "attempts": len(rows),
             "passed": sum(bool(d.get("passed")) for d, _ in rows),
             "turns_p50": pct((_number(d.get("turns")) for d, _ in rows), 0.5),
             "cost_p50_usd": pct((own for _, own in rows), 0.5), "cost_usd": round(sum(own for _, own in rows), 2)}
            for (move, went_on), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], not kv[0][1]))]


def context(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Runs a session sent, with the session's thread in their brief and without:
    the evidence on whether the scribe's context helps (observed, not a trial)."""
    sent = [r for r in found if r["has_session"] and r["session_context"] is not None]
    groups = (("with the session's thread", [r for r in sent if r["session_context"]]),
              ("without it", [r for r in sent if not r["session_context"]]))
    return [{"group": name, **_summary(rows)} for name, rows in groups if rows]


def turns(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A session's own turns and its subagents' delegations: how many, how long, what they cost."""
    tasks: dict[int, dict[str, Any]] = {}
    for row in rows:
        task = tasks.setdefault(int(row["id"]), {"source": row["source"], "status": row["status"],
                                                 "seconds": _between(row["created_at"], row["updated_at"]),
                                                 "usd": 0.0, "day": str(row["created_at"])[:10]})
        if row.get("model"):
            task["usd"] += usage.dollars(row["model"], int(row["input_tokens"] or 0), int(row["output_tokens"] or 0),
                                         int(row["cache_read"] or 0), int(row["cache_write"] or 0))
    out = {}
    for source, name in (("hook", "turns"), ("delegation", "delegations")):
        mine = [t for t in tasks.values() if t["source"] == source]
        ended = [t for t in mine if t["status"] == "done"]
        out[name] = {
            "count": len(mine), "done": len(ended), "interrupted": sum(t["status"] == "interrupted" for t in mine),
            "seconds_p50": pct((t["seconds"] for t in ended), 0.5),
            "seconds_p90": pct((t["seconds"] for t in ended), 0.9),
            "usd": round(sum(t["usd"] for t in mine), 2), "usd_p50": pct((t["usd"] for t in mine if t["usd"]), 0.5),
        }
    out["by_day"] = dict(Counter(t["day"] for t in tasks.values() if t["source"] == "hook"))
    return out


def daily(found: Sequence[Mapping[str, Any]], turn_days: Mapping[str, int]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "passed": 0, "cost_usd": 0.0, "hours": 0.0})
    for r in found:
        day = days[str(r["ended_at"] or r["started_at"])[:10]]
        day["runs"] += 1
        day["passed"] += r["status"] == "done"
        day["cost_usd"] += r["cost_usd"]
        day["hours"] += (r["wall_s"] or 0) / 3600
    for day, count in turn_days.items():
        days[day]["turns"] = count
    return [{"day": d, "turns": v.get("turns", 0), "runs": v["runs"], "passed": v["passed"],
             "cost_usd": round(v["cost_usd"], 2), "hours": round(v["hours"], 2)} for d, v in sorted(days.items())]


def compactions(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each compaction decision (`cauce.compaction`), oldest first: why, what the
    summary would cost with the cache warm beside a cold one, how it was
    delivered, and whether it ran (as settled, or as its marker was seen)."""
    from cauce import compaction

    rows: dict[int, dict[str, Any]] = {}
    for event in events:
        data = event["data"]
        if event["kind"] == compaction.DECIDED:
            warm, cold = _number(data.get("warm_usd")), _number(data.get("cold_usd"))
            tokens, window = data.get("tokens") or 0, data.get("window") or compaction.WINDOW
            rows[event["id"]] = {
                "decision": event["id"], "at": event["ts"], "session": event.get("session_id"),
                "repo": event.get("repo"), "now": bool(data.get("now")), "mode": data.get("mode"),
                "tokens": tokens, "share": round(tokens / window, 2) if window else None,
                "because": data.get("because") or [], "warm_usd": warm, "cold_usd": cold,
                "warm_share": round(warm / cold, 2) if warm is not None and cold else None,
                "delivered": None, "ran": None, "evidence": None}
            continue
        row = rows.get(int(data.get("decision") or data.get("marker") or 0))
        if row is None:
            continue
        if event["kind"] == compaction.DELIVERED:
            row["delivered"] = data.get("how")
        elif event["kind"] == compaction.SETTLED:
            row["ran"], row["evidence"] = bool(data.get("ran")), data.get("evidence")
        elif event["kind"] == compaction.SEEN and row["ran"] is None:
            row["ran"], row["evidence"] = True, f"{data.get('source')} carried its marker"
    return list(rows.values())


def compute(store: Store, *, days: int = 30, repo: str | None = None, keep_runs: int | None = 20) -> dict[str, Any]:
    """Everything the screen and the command show, for the last `days` (0: all) of
    one repository (None: every one). `keep_runs` slowest and costliest runs are
    listed; None lists every run."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
    tasks, events = store.runs_since(since, repo=repo)
    found = [r for r in runs(tasks, events) if not since or (r["ended_at"] or "") >= since]
    turn_rows = turns(store.turns_since(since, repo=repo))
    stops = Counter(r["stop"] or r["status"] for r in found if r["status"] != "done")
    moves = Counter(m for r in found for m in r["moves"] if m)
    report = {
        "days": days, "repo": repo, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": _summary(found), "phases": phases(found), "by_kind": _grouped(found, "kind"),
        "by_source": _grouped(found, "source"),
        "by_workflow": _grouped([r for r in found if r["workflow"]], "workflow"),
        "cells": cells(events, {r["task"] for r in found}), "context": context(found),
        "continuations": continuations(events, {r["task"] for r in found}),
        "stops": dict(stops.most_common()), "moves": dict(moves.most_common()),
        "turns": {k: v for k, v in turn_rows.items() if k != "by_day"},
        "daily": daily(found, turn_rows["by_day"]),
        "compactions": compactions(store.session_events(None, COMPACTION_EVENTS, since=since, repo=repo)),
        "primer": cells_mod.trial(store, repo),
        "role_revisions": revisions.overview(store, repo),
        # Whether racing a first try paid, per repository and kind (`cauce.racing`): its own window.
        "races": racing.table(store, repo),
    }
    if keep_runs is None:
        report["runs"] = found
    else:
        report["slowest"] = sorted((r for r in found if r["wall_s"]), key=lambda r: -r["wall_s"])[:keep_runs]
        report["costliest"] = sorted(found, key=lambda r: -r["cost_usd"])[:keep_runs]
    return report


def runs_csv(found: Sequence[Mapping[str, Any]]) -> str:
    """One row per run, for a spreadsheet."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(RUN_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in found:
        writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in RUN_COLUMNS})
    return out.getvalue()


def _seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}s" if value < 120 else f"{value / 60:.1f}m" if value < 7200 else f"{value / 3600:.1f}h"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def text(report: Mapping[str, Any]) -> str:
    s = report["summary"]
    scope = report["repo"] or "every repository"
    period = f"last {report['days']} days" if report["days"] else "all time"
    lines = [f"cauce stats — {scope}, {period}", ""]
    if not s["runs"]:
        lines.append("No run of cauce's yet in this period.")
    else:
        lines += [
            f"runs {s['runs']} ({s['tasks']} tasks)  passed {s['passed']} ({_rate(s['pass_rate'])})  first try "
            f"{_rate(s['first_try_rate'])}  climbed {_rate(s['climbed_rate'])}  verified {_rate(s['verified_rate'])}",
            f"time p50 {_seconds(s['wall_p50_s'])}  p90 {_seconds(s['wall_p90_s'])}  queue p50 "
            f"{_seconds(s['queued_p50_s'])}  {s['hours']}h in all, {s['minutes_per_pass'] or '—'} min per pass",
            f"cost {_usd(s['cost_usd'])}  per pass {_usd(s['cost_per_pass_usd'])}  p50 {_usd(s['cost_p50_usd'])}"
            f"  independent checks {_usd(s['verifier_cost_usd'])}  cache read {_rate(s['cache_read_share'])}",
            "",
        ]
        ph = report["phases"]
        if ph["runs"]:
            lines.append(f"where the time went ({ph['runs']} timed runs): " + "  ".join(
                f"{k[:-2]} {_seconds(ph['seconds'][k])} ({_rate(ph['share'][k])})" for k in PHASES
                if ph["seconds"][k]))
            lines.append("")
        lines.append("by kind:")
        for row in report["by_kind"]:
            lines.append(f"  {row['kind']:<16} runs {row['runs']:>4}  pass {_rate(row['pass_rate']):>4}  first "
                         f"{_rate(row['first_try_rate']):>4}  p50 {_seconds(row['wall_p50_s']):>6}  per pass "
                         f"{_usd(row['cost_per_pass_usd'])}")
        if report["context"]:
            lines += ["", "the session's thread in the brief (runs a session sent):"]
            for row in report["context"]:
                lines.append(f"  {row['group']:<26} runs {row['runs']:>4}  pass {_rate(row['pass_rate']):>4}  first "
                             f"{_rate(row['first_try_rate']):>4}  attempts {row['attempts_avg']}")
        if report.get("continuations"):
            lines += ["", "after more effort or more turns, the attempt went on in the last one's session or "
                          "started fresh:"]
            for row in report["continuations"]:
                lines.append(f"  {row['move']:<12} {'continued' if row['continued'] else 'fresh':<9} attempts "
                             f"{row['attempts']:>4}  passed {row['passed']:>4}  turns p50 {row['turns_p50'] or '—'}"
                             f"  cost p50 {_usd(row['cost_p50_usd'])}")
        primer = report.get("primer") or {}
        if any(row["tasks"] for row in primer.values()):
            lines += ["", "the cell's primer in the brief (its trial; first tries, calls before the first change):"]
            for arm, row in primer.items():
                rate = row["passed_first"] / row["tasks"] if row["tasks"] else None
                lines.append(f"  {'with it' if arm == 'primer' else 'without it':<11} tasks {row['tasks']:>4}  first "
                             f"{_rate(rate):>4}  calls before the first change p50 "
                             f"{row['calls_before_edit_p50'] if row['calls_before_edit_p50'] is not None else '—'}"
                             f"  input p50 {row['input_tokens_p50'] or '—'}")
        if report.get("role_revisions"):
            lines += ["", "the roles' lessons here (a revision on trial, its arms' first tries, and its history):"]
            lines += ["  " + line for line in revisions.text(report["role_revisions"], lessons=False)]
        if report.get("races"):
            lines += ["", "first tries that raced the next rung (each repository's last tasks of the kind):"]
            for row in report["races"]:
                lines.append(f"  {_name(row['repo']):<16} {row['kind']:<12} races {row['races']:>3}  rung won "
                             f"{row['rung_wins']:>3}  start won {row['start_wins']:>3}  both failed "
                             f"{row['neither']:>3}  {'racing' if row['pays'] else 'not racing'}: {row['why']}")
        if report["stops"]:
            lines += ["", "stopped by: " + ", ".join(f"{k} {v}" for k, v in report["stops"].items())]
    t = report["turns"]["turns"]
    if t["count"]:
        lines += ["", f"session turns {t['count']}  p50 {_seconds(t['seconds_p50'])}  p90 {_seconds(t['seconds_p90'])}"
                  f"  ${t['usd']:.2f}  interrupted {t['interrupted']}"]
    lines += _compaction_lines(report.get("compactions") or [])
    return "\n".join(lines)


def _name(repo: str | None) -> str:
    return str(repo or "-").rstrip("/").rsplit("/", 1)[-1]


def _compaction_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows:
        return []
    said = [r for r in rows if r["now"]]
    lines = ["", f"compaction decisions {len(rows)}  said now {len(said)}  delivered "
             f"{sum(1 for r in said if r['delivered'])}  ran {sum(1 for r in said if r['ran'])}"]
    for r in rows[-COMPACTIONS_SHOWN:]:
        price = (f"warm {_usd(r['warm_usd'])} vs cold {_usd(r['cold_usd'])} ({_rate(r['warm_share'])})"
                 if r["cold_usd"] else "cost not measurable")
        if r["now"]:
            outcome = (f"{r['delivered'] or 'not delivered'}, "
                       + ("ran" if r["ran"] else "did not run" if r["ran"] is False else "not seen to run"))
        else:
            outcome = next((b for b in r["because"] if b.startswith("not now: ")), "not now")
        lines.append(f"  {str(r['at'])[:16]} #{r['decision']} {r['tokens'] // 1000}k ({_rate(r['share'])})  "
                     f"{'now' if r['now'] else 'wait'}  {price}  {outcome}")
    return lines


def as_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
