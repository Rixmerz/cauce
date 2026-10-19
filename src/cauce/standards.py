"""Where a task of each size starts, from what was measured: a standard per
repository, kind and points band (`cauce.points`), and the record of how each one
moved and why.

The model is the one MECHA uses for people — standard hours per ticket type, the
real hours, and a calibration that moves the standards — here for workers. A
standard holds the start cell and the start turns, and what the finished tasks of
its band measured over the last `WINDOW` of them: how often each cell passed its
first try, what a point cost and how long it took. A calibration runs after every
`CALIBRATE_EVERY` endings in a repository (`settle`, from the hooks or the
dispatcher, never from a GET) and applies two rules, and nothing else:

- the start cell is the cheapest cell of the kind's ladder whose first tries pass
  at least `PASS_FIRST` of the time, over at least `MIN_SAMPLES` of them; it moves
  one rung per calibration, down as far as the ladder's first rung and never below
  it, and never onto a cell that needs an approval;
- the start turns are the 75th percentile of the turns the band's passing tasks
  needed, capped at `escalate.MAX_TURNS_CEILING`, one `TURNS_STEP` per calibration.

A band moves one field at a time, and not again before that change is judged: once
`JUDGE_AFTER` tasks ran under it, the window before and the window after are
compared (pass-first rate, attempts, time and cost per point), and a change that
made pass-first worse by more than `REVERT_MARGIN` is reverted, one step back, as a
change of its own. Every change and verdict is appended to the store with its rule,
its evidence and cauce's version. No model decides any of it, and money moves
nothing: a point's cost is measured and shown, never a rule's input.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any

from cauce import __version__
from cauce.escalate import MAX_TURNS_CEILING
from cauce.matrix import Cell, ladder_for, turns_for
from cauce.store import Store

#: The finished tasks of a band its measure reads, newest last.
WINDOW = 20
#: Endings in a repository between two calibrations of its standards.
CALIBRATE_EVERY = 5
#: How often a cell's first tries must pass for it to be a start, and over how many.
PASS_FIRST = 0.7
MIN_SAMPLES = 5
#: The percentile of the turns passes needed that the start turns aim at, and how far
#: one calibration moves them.
TURNS_PERCENTILE = 0.75
TURNS_STEP = 10
#: Tasks run under a change before it is judged, and how much worse pass-first may get
#: before it is reverted.
JUDGE_AFTER = 5
REVERT_MARGIN = 0.2
#: Failures that say nothing of the work: an attempt that ended only so is no first try.
NO_VERDICT = frozenset({"environment", "permission"})
#: The moves after which an attempt goes on with the same cell's try: room, or a launch that broke.
GOES_ON = frozenset({"retry", "more_turns"})
FIELDS = ("start_cell", "start_turns")
#: The last ending a settle read, and each repository's endings since its last calibration.
SEEN_KEY = "standards:seen"
#: Set once every task that ended before it had points was sized and every repository with
#: history calibrated: the standards start from the work already done, not from zero.
BACKFILL_KEY = "standards:backfilled"
#: How a size given after the fact says so.
AFTER_THE_FACT = "sized after it ended, from its text"


@dataclass(frozen=True)
class Ended:
    """A task that ended, as the standards read it."""

    id: int
    repo: str | None
    kind: str
    band: int | None
    status: str
    cost_usd: float
    wall_s: float
    pinned: bool
    attempts: tuple[dict, ...]
    #: The changes whose values its plan started with.
    used: tuple[int, ...]
    ended_at: str
    realized: int | None

    @property
    def passed(self) -> bool:
        return self.status == "done"

    @property
    def first(self) -> bool | None:
        """Whether its first attempt passed; None when that attempt said nothing of the work."""
        if not self.attempts:
            return None
        a = self.attempts[0]
        if not a["passed"] and a.get("failure") in NO_VERDICT:
            return None
        return bool(a["passed"])


def ended(store: Store, *, repo: str | None = None, since: str = "", until: str | None = None) -> list[Ended]:
    """The tasks that ended, oldest first: the person's work, an investigator's reading aside."""
    rows = [r for r in store.ended_tasks(repo=repo, since=since, until=until)
            if '"investigation"' not in (r.get("options") or "")]
    attempts = store.attempts_of([r["id"] for r in rows])
    out = []
    for r in rows:
        used = ((r["planned_data"] or {}).get("standard") or {}).get("changes") or ()
        out.append(Ended(int(r["id"]), r["repo"], r["kind"] or "", r.get("points"), r["status"],
                         float(r["cost_usd"] or 0), float(r["wall_s"] or 0), bool(r["pinned"]),
                         tuple(attempts.get(int(r["id"]), ())), tuple(int(c) for c in used), r["ended_at"],
                         r["ended_data"].get("realized")))
    return out


def _label(text: str | None) -> str | None:
    try:
        return Cell.parse(text).label if text else None
    except ValueError:
        return None


def samples(attempts: Sequence[Mapping[str, Any]]) -> list[tuple[str, bool]]:
    """Each first try of a cell in a task's attempts, and whether it passed. A first try is
    the task's first attempt, or the first after a new model, which starts from the base;
    more effort continues the same session, so its cell has no first try of its own. A try
    goes on through the attempts on its cell after a retry or more turns (room, or a launch
    that broke, never the cell's fault) and passes when one of them did. A try that ended
    only on the environment or a refusal says nothing of the work: it is no sample."""
    out: list[list[Any]] = []
    current: list[Any] | None = None
    before = None
    for i, a in enumerate(attempts):
        cell = _label(a.get("cell"))
        if i == 0 or before == "next_model":
            current = [cell, False, False]
            out.append(current)
        elif current is None or cell != current[0] or before not in GOES_ON:
            current = None
        if current is not None:
            current[1] = current[1] or bool(a["passed"])
            current[2] = current[2] or bool(a["passed"]) or a.get("failure") not in NO_VERDICT
        before = a.get("move")
    return [(cell, passed) for cell, passed, said in out if said and cell]


def turns_needed(attempts: Sequence[Mapping[str, Any]]) -> int | None:
    """The turns a passing task needed: its passing attempt's and those of the attempts right
    before it on the same cell that ran out of turns. None for no pass, or none recorded."""
    found = [i for i, a in enumerate(attempts) if a["passed"]]
    if not found:
        return None
    i = found[-1]
    total = int(attempts[i].get("turns") or 0)
    j = i - 1
    while j >= 0 and attempts[j].get("cell") == attempts[i].get("cell") and \
            attempts[j].get("failure") == "turns_exhausted":
        total += int(attempts[j].get("turns") or 0)
        j -= 1
    return total or None


def rates(tasks: Sequence[Ended]) -> dict[str, list[int]]:
    """Per cell, [passed, tried] over its first tries. A cell a person pinned says nothing of
    where the router should start, so a pinned task teaches none."""
    out: dict[str, list[int]] = {}
    for task in tasks:
        if task.pinned:
            continue
        for cell, passed in samples(task.attempts):
            row = out.setdefault(cell, [0, 0])
            row[0] += int(passed)
            row[1] += 1
    return out


def metrics(tasks: Sequence[Ended]) -> dict[str, Any]:
    """What a set of tasks measured: how many, how many passed, how often the first attempt
    passed, attempts on average, and what a point that passed cost and took, the tasks that
    did not pass included."""
    sized = [t for t in tasks if t.band]
    points = sum(t.band or 0 for t in sized if t.passed)
    first = [f for f in (t.first for t in tasks) if f is not None]
    return {
        "tasks": len(tasks), "passed": sum(t.passed for t in tasks),
        "pass_first": round(sum(first) / len(first), 3) if first else None,
        "attempts": round(sum(len(t.attempts) for t in tasks) / len(tasks), 2) if tasks else None,
        "cost_per_point": round(sum(t.cost_usd for t in sized) / points, 4) if points else None,
        "time_per_point_s": round(sum(t.wall_s for t in sized) / points, 1) if points else None,
    }


def _position(rungs: Sequence[Cell], cell: Cell | None) -> int:
    if cell is None:
        return 0
    if cell in rungs:
        return rungs.index(cell)
    below = [i for i, c in enumerate(rungs) if c.sort_key() <= cell.sort_key()]
    return below[-1] if below else 0


def start_cell(ladder: Sequence[Cell], current: Cell | None,
               measured: Mapping[str, Sequence[int]]) -> tuple[Cell | None, dict[str, Any]]:
    """Where the start cell moves (None: it stays), and the evidence. `current` None is
    cauce's own rules, which start at the ladder's first rung."""
    rungs = [c for c in ladder if not c.needs_approval]
    cells = {c.label: list(measured[c.label]) for c in rungs if c.label in measured}
    evidence: dict[str, Any] = {"cells": cells, "rates": {k: round(p / n, 3) for k, (p, n) in cells.items() if n},
                                "threshold": PASS_FIRST, "min_samples": MIN_SAMPLES, "target": None}
    qualified = [i for i, c in enumerate(rungs) if c.label in cells and cells[c.label][1] >= MIN_SAMPLES
                 and cells[c.label][0] / cells[c.label][1] >= PASS_FIRST]
    if not qualified:
        return None, evidence
    target, at = qualified[0], _position(rungs, current)
    evidence["target"] = rungs[target].label
    if target == at:
        return None, evidence
    return rungs[at + (1 if target > at else -1)], evidence


def _rank(values: Sequence[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def start_turns(current: int, needed: Sequence[int]) -> tuple[int | None, dict[str, Any]]:
    """Where the start turns move (None: they stay), and the evidence."""
    evidence: dict[str, Any] = {"samples": len(needed), "min_samples": MIN_SAMPLES, "p75": None, "step": TURNS_STEP}
    if len(needed) < MIN_SAMPLES:
        return None, evidence
    aim = min(_rank(needed, TURNS_PERCENTILE), MAX_TURNS_CEILING)
    evidence["p75"] = aim
    if aim == current:
        return None, evidence
    return min(max(current + max(-TURNS_STEP, min(TURNS_STEP, aim - current)), 1), MAX_TURNS_CEILING), evidence


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


def _open(store: Store, repo: str) -> list[dict]:
    """The changes in this repository that wait for their verdict (a revert is judged by none)."""
    verdicts = store.verdicts()
    return [c for c in store.calibrations(repo=repo) if c["reverts"] is None and c["id"] not in verdicts]


def _record(store: Store, **change: Any) -> dict[str, Any]:
    change["version"] = __version__
    return {**change, "id": store.add_calibration(**change)}


def calibrate(store: Store, repo: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Measure every band of the repository's standards over its window, and move each by the
    rules: the start cell first, else the start turns; a band whose last change waits for its
    verdict does not move. The changes made, as recorded."""
    at = _stamp(now)
    bands: dict[tuple[str, int], list[Ended]] = defaultdict(list)
    for task in ended(store, repo=repo):
        if task.band and task.kind:
            bands[(task.kind, task.band)].append(task)
    waiting = {(c["kind"], c["band"]) for c in _open(store, repo)}
    changes = []
    for (kind, band), tasks in sorted(bands.items()):
        window = tasks[-WINDOW:]
        cells = rates(window)
        row = store.standard(repo, kind, band) or {}
        store.set_standard(repo, kind, band, measured={**metrics(window), "cells": cells})
        if (kind, band) in waiting:
            continue
        moved, evidence = start_cell(ladder_for(kind), _cell(row.get("start_cell")), cells)
        if moved is not None:
            target = evidence["target"]
            rule = (f"the cheapest rung of the {kind} ladder whose first tries passed at least {PASS_FIRST:.0%} of "
                    f"{MIN_SAMPLES} or more is {target}" + ("" if moved.label == target else "; one rung toward it"))
            changes.append(_record(store, at=at, repo=repo, kind=kind, band=band, field="start_cell",
                                   old_value=row.get("start_cell"), new_value=moved.label, rule=rule,
                                   evidence={**evidence, "tasks": len(window)}))
            store.set_standard(repo, kind, band, start_cell=moved.label)
            continue
        needed = [n for n in (turns_needed(t.attempts) for t in window if t.passed) if n]
        current = row.get("start_turns")
        turns, evidence = start_turns(current or turns_for(kind), needed)
        if turns is not None:
            rule = (f"the 75th percentile of the turns {len(needed)} passing tasks needed is {evidence['p75']}"
                    + ("" if turns == evidence["p75"] else f"; one step of {TURNS_STEP} toward it"))
            changes.append(_record(store, at=at, repo=repo, kind=kind, band=band, field="start_turns",
                                   old_value=str(current) if current else None, new_value=str(turns), rule=rule,
                                   evidence={**evidence, "tasks": len(window)}))
            store.set_standard(repo, kind, band, start_turns=turns)
    return changes


def backfill(store: Store, ids: Sequence[int] | None = None) -> int:
    """Give a size to tasks that ended without one (planned before cauce kept points, or by
    a plan that failed before it sized them): the same pure estimate a plan makes
    (`points.of_request`), from the task's text, kind and the risk its plan recorded (read
    again from its text when it recorded none), marked as given after the fact. A task with
    no kind stays out. How many were sized."""
    from cauce import points as points_mod
    from cauce import risk as risk_mod

    if ids is None:
        rows = store.ended_tasks(limit=100000)
    else:
        rows = []
        for task_id in ids:
            task = store.get_task(task_id)
            planned = store.events_of(task_id, "planned") if task else []
            if task:
                rows.append({**task, "planned_data": (planned[0]["data"] or {}) if planned else {}})
    sized = 0
    for row in rows:
        if row.get("points") or not row.get("kind") or row.get("source") not in ("cauce", "queue"):
            continue
        recorded = (row["planned_data"] or {}).get("risk") or {}
        risk = (risk_mod.Risk(str(recorded.get("tier") or "medium"), list(recorded.get("reasons") or []))
                if recorded else risk_mod.of_request(row.get("body") or "", row["kind"]))
        score = points_mod.of_request(row.get("body") or row.get("title") or "", row["kind"], risk)
        store.update_task(int(row["id"]), points=score.points,
                          points_why=", ".join([AFTER_THE_FACT, *score.reasons]))
        sized += 1
    return sized


def _cell(label: str | None) -> Cell | None:
    return Cell.parse(label) if _label(label) else None


def judge(store: Store, repo: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Judge each change that ran under `JUDGE_AFTER` tasks: the window before it against the
    tasks after it. Worse pass-first by more than `REVERT_MARGIN` reverts it, one step back,
    recorded as a change that names it; anything else keeps it. The verdicts made."""
    pending = _open(store, repo)
    if not pending:
        return []
    at = _stamp(now)
    tasks = [t for t in ended(store, repo=repo) if t.band]
    out = []
    for change in pending:
        band = [t for t in tasks if t.kind == change["kind"] and t.band == change["band"]]
        # A change carried from a repository that joined a workspace is judged by the tasks
        # that used it under either id.
        ids = {change["id"], json.loads(change.get("evidence") or "{}").get("carried_from")}
        after = [t for t in band if ids & set(t.used)][:JUDGE_AFTER]
        if len(after) < JUDGE_AFTER:
            continue
        before, then = metrics([t for t in band if t.ended_at < change["at"]][-WINDOW:]), metrics(after)
        worse = before["pass_first"] is not None and then["pass_first"] is not None and \
            round(before["pass_first"] - then["pass_first"], 6) > REVERT_MARGIN
        verdict = "reverted" if worse else "kept"
        reason = (f"pass-first {_pct(before['pass_first'])} in the {before['tasks']} tasks before, "
                  f"{_pct(then['pass_first'])} in the {then['tasks']} after")
        store.add_verdict(at=at, change_id=change["id"], verdict=verdict, window_before=before, window_after=then,
                          reason=reason, version=__version__)
        if worse:
            _record(store, at=at, repo=repo, kind=change["kind"], band=change["band"], field=change["field"],
                    old_value=change["new_value"], new_value=change["old_value"], reverts=change["id"],
                    rule=f"revert #{change['id']}: it made pass-first worse by more than {REVERT_MARGIN:.0%} "
                         f"({reason})",
                    evidence={"before": before, "after": then, "margin": REVERT_MARGIN})
            value = change["old_value"]
            store.set_standard(repo, change["kind"], change["band"], **{change["field"]: (
                int(value) if value and change["field"] == "start_turns" else value)})
        out.append({"change": change["id"], "repo": repo, "verdict": verdict, "before": before, "after": then})
    return out


def settle(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """What the endings since the last settle are due: the verdicts of changes that ran long
    enough, and a calibration of each repository that ended `CALIBRATE_EVERY` tasks since its
    last. In one write transaction: a hook and the dispatcher at the same moment never
    calibrate twice. Runs in the hooks and the dispatcher, never in a GET."""
    calibrated: list[dict[str, Any]] = []
    judged: list[dict[str, Any]] = []
    if not store.meta(BACKFILL_KEY):
        # The first settle on a store with history: what ended before points existed is
        # sized, and every repository with sized endings calibrates now, not after five more.
        with store.immediate():
            if not store.meta(BACKFILL_KEY):
                sized = backfill(store)
                for repo in sorted({t.repo for t in ended(store) if t.repo and t.band}):
                    calibrated += calibrate(store, repo, now=now)
                    store.set_meta(f"standards:since:{repo}", "0")
                store.set_meta(BACKFILL_KEY, json.dumps({"at": _stamp(now), "sized": sized,
                                                         "version": __version__}))
    if not store.endings_after(int(store.meta(SEEN_KEY) or 0)):
        # Nothing ended since: a turn's end takes no write lock for nothing.
        return {"ended": 0, "calibrated": calibrated, "judged": judged}
    with store.immediate():
        endings = store.endings_after(int(store.meta(SEEN_KEY) or 0))
        if not endings:
            return {"ended": 0, "calibrated": calibrated, "judged": judged}
        store.set_meta(SEEN_KEY, str(endings[-1]["id"]))
        repos: dict[str, set[int]] = defaultdict(set)
        for e in endings:
            if e["repo"] and '"investigation"' not in (e["options"] or ""):
                repos[e["repo"]].add(int(e["task_id"]))
        # One that ended with no size (a plan that failed before it sized it) is sized now.
        backfill(store, ids=[int(e["task_id"]) for e in endings])
        for repo, ids in sorted(repos.items()):
            judged += judge(store, repo, now=now)
            count = int(store.meta(f"standards:since:{repo}") or 0) + len(ids)
            if count >= CALIBRATE_EVERY:
                calibrated += calibrate(store, repo, now=now)
                count = 0
            store.set_meta(f"standards:since:{repo}", str(count))
    return {"ended": len(endings), "calibrated": calibrated, "judged": judged}


# --- what a plan takes from its standard, and how a change reads -----------------------------------

def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.0f} %"


def day(at: str, tz: tzinfo | None = None) -> str:
    """A recorded time as the person reads a date: `Oct 20`, in their time zone (None: this machine's)."""
    moment = datetime.fromisoformat(at).astimezone(tz)
    return f"{moment:%b} {moment.day}"


def size(band: int) -> str:
    return f"{band} point" if band == 1 else f"{band} points"


def because(change: Mapping[str, Any]) -> str:
    """The evidence of a change, in a sentence: "2 of 8 passed first at sonnet/xhigh and 4 of 5
    at opus/high", or the turns passes needed, or what a revert found."""
    evidence = json.loads(change["evidence"]) if isinstance(change["evidence"], str) else change["evidence"]
    if change.get("reverts"):
        return change["rule"]
    if change["field"] == "start_cell":
        cells = [f"{p} of {n} " + ("passed first at " if i == 0 else "at ") + label
                 for i, (label, (p, n)) in enumerate((evidence.get("cells") or {}).items())]
        return " and ".join(cells) or change["rule"]
    return (f"the passing tasks needed {evidence.get('p75')} turns at the 75th percentile "
            f"({evidence.get('samples')} of them)")


@dataclass(frozen=True)
class Used:
    """What a plan takes from the standard of its band: its cell and turns (None: cauce's own
    rules, or a person's flag that wins), the changes that set them, and why."""

    band: int
    cell: Cell | None = None
    turns: int | None = None
    changes: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def data(self) -> dict[str, Any]:
        return {"band": self.band, "cell": self.cell.label if self.cell else None, "turns": self.turns,
                "changes": list(self.changes)}


def for_plan(store: Store, repo: str, kind: str, band: int, *, pinned: bool, explicit_turns: bool) -> Used:
    """The standard a task of this kind and size starts on here. A cell a person pinned, or
    turns they asked for, always win; a cell the ladder no longer holds is none."""
    row = store.standard(repo, kind, band) or {}
    latest = {c["field"]: c for c in store.calibrations(repo=repo) if c["kind"] == kind and c["band"] == band}
    name = f"{kind} at {size(band)} here"
    reasons, changes = [], []
    cell = _cell(row.get("start_cell"))
    if cell is not None and (cell not in ladder_for(kind) or cell.needs_approval):
        reasons.append(f"the standard for {name} says {cell.label}, which the {kind} ladder no longer holds: "
                       "cauce's rules choose")
        cell = None
    if cell is not None and pinned:
        reasons.append(f"the standard for {name} starts at {cell.label}; the cell pinned for this task wins")
        cell = None
    turns = row.get("start_turns")
    if turns and explicit_turns:
        reasons.append(f"the standard for {name} starts with {turns} turns; the turns asked for this task win")
        turns = None
    for field, value, what in (("start_cell", cell, f"starts at {cell.label if cell else ''}"),
                               ("start_turns", turns, f"starts with {turns} turns")):
        change = latest.get(field)
        if value and change:
            changes.append(int(change["id"]))
            reasons.append(f"standard for {name}: {what} since {day(change['at'])} "
                           f"(change #{change['id']}: {because(change)})")
    if not row.get("start_cell") and not row.get("start_turns") and not pinned:
        # A pinned cell says where it starts; the rules choose only what nobody pinned.
        reasons.append(f"no standard for {name} yet: cauce's rules choose"
                       + (f" (measured over {_measured(row)} tasks so far)" if row else ""))
    return Used(band, cell, turns or None, tuple(changes), tuple(reasons))


def _measured(row: Mapping[str, Any]) -> int:
    return int(json.loads(row.get("measured") or "{}").get("tasks") or 0)
