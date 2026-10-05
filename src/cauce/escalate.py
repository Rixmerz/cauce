"""After a failed attempt: which way to move, if any.

A failure is evidence about *where* the problem lives, and each place has one
right response. Conflating them is how an orchestrator burns a budget going in
circles:

- **Retry** — the work never happened (a timeout, a missing binary). Same cell,
  once. Escalating it spends the top tier on a problem no model would solve.
- **More effort** — the work happened and was shallow: a missed edge case, a
  bug left behind, no verification. Same model, the next effort on the ladder.
- **Next model** — the work happened and was *wrong*: the approach was off, or
  two cells already gave the same answer. More thinking from the same model is a
  bet that is already settled; the next model enters at its own cell.
- **More turns** — the worker hit its turn ceiling. A ceiling is not a fault and
  does not clear on its own; it is raised once, then the task is split.
- **Replan** — the spec or the architecture is wrong. No model fixes a task that
  should not exist; the run stops and says so.

Deliberately absent from the inputs: the model's stated confidence. A model's
opinion of its own output is not evidence about the output.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from cauce.matrix import Cell
from cauce.text import reads_alike


class Failure(StrEnum):
    CODE_BUG = "code_bug"  # attempted, left a bug or an edge case
    TEST_BUG = "test_bug"  # attempted, the test it relied on was wrong
    INCONCLUSIVE = "inconclusive"  # no verdict, or a pass it could not show
    APPROACH = "approach"  # attempted, misunderstood or went the wrong way
    SPEC_BUG = "spec_bug"  # the task as written cannot be done
    ARCHITECTURE_BUG = "architecture_bug"
    ENVIRONMENT = "environment"  # never attempted: timeout, missing tool, transport
    TURNS_EXHAUSTED = "turns_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"  # the attempt's own spending cap


EFFORT_AXIS = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.INCONCLUSIVE, Failure.BUDGET_EXHAUSTED})
REPLAN = frozenset({Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG})


class Move(StrEnum):
    RETRY = "retry"
    MORE_EFFORT = "more_effort"
    NEXT_MODEL = "next_model"
    MORE_TURNS = "more_turns"
    REPLAN = "replan"
    NEEDS_APPROVAL = "needs_approval"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"


#: The moves after which another attempt runs.
CONTINUES = frozenset({Move.RETRY, Move.MORE_EFFORT, Move.NEXT_MODEL, Move.MORE_TURNS})

MAX_ENV_RETRIES = 1
MAX_TURNS_CEILING = 200


@dataclass(frozen=True)
class Attempt:
    cell: Cell
    max_turns: int
    passed: bool
    failure: Failure | None = None
    summary: str = ""


@dataclass(frozen=True)
class Decision:
    move: Move
    cell: Cell | None = None
    max_turns: int = 0
    reason: str = ""

    @property
    def continues(self) -> bool:
        return self.move in CONTINUES


def repeated_answer(attempts: Sequence[Attempt]) -> bool:
    """The last two failures, at different cells, said the same thing.

    Same model and effort agreeing with itself is determinism, not a finding,
    so only a pair from different cells counts.
    """
    failed = [a for a in attempts if not a.passed]
    if len(failed) < 2:
        return False
    a, b = failed[-2], failed[-1]
    return a.cell != b.cell and a.failure == b.failure and reads_alike(a.summary, b.summary)


def _position(ladder: Sequence[Cell], cell: Cell) -> int:
    try:
        return ladder.index(cell)
    except ValueError:
        # A pinned or learned cell outside this ladder: place it where its model
        # and effort would sort, so the next move still climbs.
        key = cell.sort_key()
        return sum(1 for c in ladder if c.sort_key() < key) - 1


def _next_effort(ladder: Sequence[Cell], cell: Cell) -> Cell | None:
    for candidate in ladder[_position(ladder, cell) + 1 :]:
        if candidate.model == cell.model:
            return candidate
        return None
    return None


def _next_model(ladder: Sequence[Cell], cell: Cell) -> Cell | None:
    for candidate in ladder:
        if candidate.model_rank() > cell.model_rank():
            return candidate
    return None


def decide(
    ladder: Sequence[Cell],
    attempts: Sequence[Attempt],
    *,
    allow_approval: bool = False,
) -> Decision:
    """The move after the last attempt."""
    if not attempts:
        raise ValueError("decide() needs at least one attempt")
    last = attempts[-1]
    if last.passed:
        raise ValueError("the last attempt passed; there is nothing to decide")
    failure = last.failure or Failure.INCONCLUSIVE

    if failure in REPLAN:
        return Decision(Move.REPLAN, reason=f"{failure}: the task, not the work, is wrong")

    if failure is Failure.ENVIRONMENT:
        retries = sum(1 for a in attempts if a.failure is Failure.ENVIRONMENT)
        if retries <= MAX_ENV_RETRIES:
            return Decision(Move.RETRY, last.cell, last.max_turns, "the work never ran; same cell")
        return Decision(Move.BLOCKED, reason="the environment failed twice; fix it, then resume")

    if failure is Failure.TURNS_EXHAUSTED:
        raised = sum(1 for a in attempts if a.failure is Failure.TURNS_EXHAUSTED)
        if raised <= 1 and last.max_turns < MAX_TURNS_CEILING:
            turns = min(last.max_turns * 2, MAX_TURNS_CEILING)
            return Decision(Move.MORE_TURNS, last.cell, turns, f"hit {last.max_turns} turns; raised to {turns}")
        return Decision(Move.REPLAN, reason="the task does not fit even the raised turn budget; split it")

    if repeated_answer(attempts) or failure is Failure.APPROACH:
        why = "two cells gave the same answer" if failure is not Failure.APPROACH else "the approach was wrong"
        target = _next_model(ladder, last.cell)
        if target is None:
            return Decision(Move.REPLAN, reason=f"{why}, and there is no stronger model on this ladder")
        return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval)

    # Effort axis: the work was shallow. The next effort of the same model,
    # and only when this model has run out does the next one start.
    target = _next_effort(ladder, last.cell)
    if target is not None:
        return Decision(Move.MORE_EFFORT, target, last.max_turns, f"{failure}: same model, more thorough")
    target = _next_model(ladder, last.cell)
    if target is None:
        return Decision(Move.EXHAUSTED, reason="the top of the ladder failed too")
    why = f"{failure}, and {last.cell.label} was this model's last cell"
    return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval)


def _gated(move: Move, cell: Cell, turns: int, why: str, allow_approval: bool) -> Decision:
    if cell.needs_approval and not allow_approval:
        return Decision(Move.NEEDS_APPROVAL, cell, turns, f"{why}; {cell.label} needs your approval")
    return Decision(move, cell, turns, why)
