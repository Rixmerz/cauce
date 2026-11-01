"""Wide or deep, by the race's own record: whether racing a first try paid where it ran.

A first try races the next rung (`orchestrate.race_cell`, wide) where the start rarely
passes first; without a race, a failed try climbs, and on the same model it goes on in its
own session (deep). Which one is better here is measured, never assumed: once at least
`JUDGE_AFTER` races ran among a repository's last `standards.WINDOW` tasks of a kind, each
race is read from its attempts (`outcome`), and racing stays only where it paid:

- the rung won (it passed while the start had not) at least `WINS_BELOW` of the races;
- and, when both sides were measured (at least `TIME_SAMPLES` each), the races whose start
  did not win took less wall time to pass, at the median, than the comparable climbs
  without a race (tasks of the kind that passed after their first try failed).

Where it did not pay, the start does not race, the climb goes deep instead, and the plan
says why. The judgement reads a window, so a repository that stopped racing has its races
fall out of the window and is tried again once fewer than `JUDGE_AFTER` remain. No model
reads any of it, and money is never an input: a race's second worker is its price, and the
question is only whether the work got anywhere sooner or more often.
"""
from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from locks import standards
from locks.standards import Ended
from locks.store import Store

#: Races among the window before racing is judged by its record.
JUDGE_AFTER = 5
#: A rung that won fewer races than this share bought a second worker mostly for nothing.
WINS_BELOW = 0.25
#: The passing tasks each side needs before the time to pass counts in the judgement.
TIME_SAMPLES = 3

#: How a race came out: the rung passed first, the start passed first, or neither passed.
RUNG, START, NEITHER = "rung", "start", "neither"
RACED_OUT = "raced_out"
#: The rung's try of a race neither side won, kept beside the start's, which went on: its own
#: first try, and the mark that the race happened (the start's row takes the next move).
RACED_BESIDE = "raced_beside"


def outcome(attempts: Sequence[Mapping[str, Any]]) -> str | None:
    """How a task's race came out, read from its attempts; None when its first try did not
    race. A race leaves two attempts first: the start's stopped one and the rung's when the
    rung passed first; the start's and the rung's stopped one when the start did; the start's
    and the rung's (moved `raced_beside`) when neither passed; older records kept `race` on the
    start's, which the next move then overwrote."""
    if len(attempts) < 2:
        return None
    first, second = attempts[0].get("move"), attempts[1].get("move")
    if first == RACED_OUT:
        return RUNG
    if second == RACED_OUT:
        return START
    if second == RACED_BESIDE:
        return NEITHER
    if first == "race":
        return NEITHER
    return None


@dataclass(frozen=True)
class Record:
    """What the races of one repository and kind came to, and whether racing stays on there."""

    races: int
    rung_wins: int
    start_wins: int
    neither: int
    #: Median wall time to pass of the races the start did not win, and of the climbs; None unmeasured.
    raced_p50_s: float | None
    climb_p50_s: float | None
    #: Whether its first tries keep racing here, and why.
    pays: bool
    why: str

    def data(self) -> dict[str, Any]:
        return asdict(self)


def _p50(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 1) if len(values) >= TIME_SAMPLES else None


def judge(window: Sequence[Ended], history: Sequence[Ended] = ()) -> Record:
    """Whether racing paid over `window` (a repository's last tasks of one kind, oldest
    first), with the climbs to compare taken from `history` (every task of that kind there;
    the window when empty)."""
    found = [(t, outcome(t.attempts)) for t in window]
    raced = [(t, o) for t, o in found if o is not None]
    rung = sum(o == RUNG for _, o in raced)
    start = sum(o == START for _, o in raced)
    neither = len(raced) - rung - start
    raced_p50 = _p50([t.wall_s for t, o in raced if o != START and t.passed and t.wall_s > 0])
    climbs = [t for t in (history or window) if outcome(t.attempts) is None and t.passed and t.first is False
              and t.wall_s > 0][-standards.WINDOW:]
    climb_p50 = _p50([t.wall_s for t in climbs])
    tally = f"the rung won {rung} of {len(raced)} races here (the start {start}, neither {neither})"
    timed = (f"; races passed in {raced_p50:.0f}s at the median against {climb_p50:.0f}s for climbs without one"
             if raced_p50 is not None and climb_p50 is not None else "")
    if len(raced) < JUDGE_AFTER:
        return Record(len(raced), rung, start, neither, raced_p50, climb_p50, True,
                      f"{len(raced)} race(s) so far here, judged after {JUDGE_AFTER}")
    if rung / len(raced) < WINS_BELOW:
        return Record(len(raced), rung, start, neither, raced_p50, climb_p50, False,
                      f"{tally}{timed}: racing rarely pays, so a failed first try climbs deep instead")
    if raced_p50 is not None and climb_p50 is not None and raced_p50 >= climb_p50:
        return Record(len(raced), rung, start, neither, raced_p50, climb_p50, False,
                      f"{tally}{timed}: racing saved no time, so a failed first try climbs deep instead")
    return Record(len(raced), rung, start, neither, raced_p50, climb_p50, True, f"racing pays: {tally}{timed}")


def of(tasks: Sequence[Ended], kind: str) -> Record:
    """The record of one kind among a repository's ended tasks (oldest first)."""
    mine = [t for t in tasks if t.kind == kind]
    return judge(mine[-standards.WINDOW:], mine)


def table(store: Store, repo: str | None = None) -> list[dict[str, Any]]:
    """Per repository and kind where a race ran in the window: its record (`locks stats`)."""
    tasks = standards.ended(store, repo=repo)
    pairs = sorted({(t.repo or "", t.kind) for t in tasks if t.kind})
    rows = []
    for where, kind in pairs:
        record = of([t for t in tasks if (t.repo or "") == where], kind)
        if record.races:
            rows.append({"repo": where, "kind": kind, **record.data()})
    return rows
