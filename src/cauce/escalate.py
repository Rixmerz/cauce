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
- **Blocked** — a person has to act: the environment failed twice, or the
  worker was refused a command (a refusal is a setting, so retrying it, or
  sending a stronger model into it, gets the same refusal).

Deliberately absent from the inputs: the model's stated confidence. A model's
opinion of its own output is not evidence about the output.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from cauce.matrix import EFFORT_PURPOSE, MODELS, Cell
from cauce.text import reads_alike


class Failure(StrEnum):
    CODE_BUG = "code_bug"  # attempted, left a bug or an edge case
    TEST_BUG = "test_bug"  # attempted, the test it relied on was wrong
    INCONCLUSIVE = "inconclusive"  # no verdict, or a pass it could not show
    APPROACH = "approach"  # attempted, misunderstood or went the wrong way
    SPEC_BUG = "spec_bug"  # the task as written cannot be done
    ARCHITECTURE_BUG = "architecture_bug"
    ENVIRONMENT = "environment"  # never attempted: timeout, missing tool, transport
    PERMISSION = "permission"  # a tool call the worker needed was refused by the permission settings
    TURNS_EXHAUSTED = "turns_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"  # the attempt's own spending cap


#: What each failure says about where the problem lives, as a move explains itself.
MEANING = {
    Failure.CODE_BUG: "the work happened and left a bug or a missed edge case",
    Failure.TEST_BUG: "the work happened, but the test it relied on was wrong",
    Failure.INCONCLUSIVE: "no verdict, or a pass without evidence",
    Failure.APPROACH: "the work went the wrong way: the task was misunderstood",
    Failure.SPEC_BUG: "the task as written cannot be done",
    Failure.ARCHITECTURE_BUG: "the design the task assumes is wrong",
    Failure.ENVIRONMENT: "the work never ran: a timeout, a missing tool, a transport error",
    Failure.PERMISSION: "a tool call the worker needed was refused by the permission settings",
    Failure.TURNS_EXHAUSTED: "the worker used all its turns before a verdict",
    Failure.BUDGET_EXHAUSTED: "the attempt reached its own spending cap before a verdict",
}

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
    CONVERGED = "converged"


#: The moves after which another attempt runs.
CONTINUES = frozenset({Move.RETRY, Move.MORE_EFFORT, Move.NEXT_MODEL, Move.MORE_TURNS})
#: Which dial a move turns; every other move stops the run.
AXIS = {Move.RETRY: "retry", Move.MORE_EFFORT: "effort", Move.NEXT_MODEL: "model", Move.MORE_TURNS: "turns"}

MAX_ENV_RETRIES = 1
MAX_TURNS_CEILING = 200


@dataclass(frozen=True)
class Attempt:
    cell: Cell
    max_turns: int
    passed: bool
    failure: Failure | None = None
    summary: str = ""
    #: The tool calls the permission settings refused, as `Bash(npm run build)`.
    denied: tuple[str, ...] = ()
    #: The `--allow` rules that would let them through, one per program.
    allow: tuple[str, ...] = ()
    #: What this attempt itself changed: work that moved, even when it ran out.
    changed: tuple[str, ...] = ()
    #: The output that failed it, for the next attempt's brief.
    evidence: str = ""


@dataclass(frozen=True)
class Decision:
    move: Move
    cell: Cell | None = None
    max_turns: int = 0
    reason: str = ""
    #: The chain of evidence behind the move, in order: what the attempt ended
    #: with, what that means, the rule it was read against, where that leads.
    because: tuple[str, ...] = ()
    #: Cells of the ladder the move went past, each with why.
    skipped: tuple[str, ...] = ()

    @property
    def continues(self) -> bool:
        return self.move in CONTINUES

    @property
    def axis(self) -> str:
        return AXIS.get(self.move, "stop")


def repeated_answer(attempts: Sequence[Attempt]) -> bool:
    """The last two failures, at different cells, said the same thing.

    Same model and effort agreeing with itself is determinism, not a finding,
    so only a pair from different cells counts.
    """
    return _repeated_pair(attempts) is not None


def _repeated_pair(attempts: Sequence[Attempt]) -> tuple[int, int] | None:
    """The 1-based numbers of the two failures that said the same thing."""
    failed = [(i, a) for i, a in enumerate(attempts, 1) if not a.passed]
    if len(failed) < 2:
        return None
    (i, a), (j, b) = failed[-2], failed[-1]
    if a.cell != b.cell and a.failure == b.failure and reads_alike(a.summary, b.summary):
        return i, j
    return None


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


def _skipped(ladder: Sequence[Cell], cell: Cell, target: Cell) -> tuple[str, ...]:
    """The cells between where the work was and where the move goes, all of the
    same model: more effort from a model whose approach was the problem."""
    if target not in ladder:
        return ()
    between = ladder[_position(ladder, cell) + 1: ladder.index(target)]
    return tuple(f"{c.label}: more effort from {c.model}, which is not what failed" for c in between
                 if c.model == cell.model)


def _arrives(target: Cell) -> str:
    purpose = f" ({EFFORT_PURPOSE[target.effort]})" if target.effort else ""
    return f"next: {target.label}{purpose}"


def decide(
    ladder: Sequence[Cell],
    attempts: Sequence[Attempt],
    *,
    allow_approval: bool = False,
    reading: bool = False,
) -> Decision:
    """The move after the last attempt, with the evidence it rests on. `reading`
    is a task that only finds out: its answer is what it is for."""
    if not attempts:
        raise ValueError("decide() needs at least one attempt")
    last = attempts[-1]
    if last.passed:
        raise ValueError("the last attempt passed; there is nothing to decide")
    failure = last.failure or Failure.INCONCLUSIVE
    seen = f"attempt {len(attempts)} at {last.cell.label} ended {failure}: {MEANING[failure]}"

    if failure in REPLAN:
        return Decision(Move.REPLAN, reason=f"{failure}: the task, not the work, is wrong",
                        because=(seen, "no model fixes a task that should not exist: it is rewritten or split, "
                                       "not climbed"))

    if failure is Failure.PERMISSION or last.denied:
        # A refusal is the setting that stopped the work, whatever else the
        # attempt ended with: a check that went red because the worker could not
        # run its tools is not shallow work, and more effort meets the same refusal.
        more = len(last.denied) - 3
        refused = (", ".join(last.denied[:3]) + (f" and {more} more" if more > 0 else "")) or "a tool call"
        grant = " ".join(f"--allow '{rule}'" for rule in last.allow)
        how = f"allow it ({grant})" if grant else "allow it (`--allow`)"
        return Decision(Move.BLOCKED, reason=f"the worker was refused {refused}; {how}, then resume",
                        because=(seen, f"refused: {', '.join(last.denied) or 'a tool call'}",
                                 "a refusal is a setting: a retry, or a stronger model, gets the same refusal",
                                 *([f"the rules that let it through, one per program: {', '.join(last.allow)}"]
                                   if last.allow else [])))

    if failure is Failure.ENVIRONMENT:
        retries = sum(1 for a in attempts if a.failure is Failure.ENVIRONMENT)
        if retries <= MAX_ENV_RETRIES:
            return Decision(Move.RETRY, last.cell, last.max_turns, "the work never ran; same cell",
                            because=(seen, "a retry is not an escalation: nothing was tried, so nothing says a "
                                           "stronger cell would do better; the same cell, once",
                                     f"next: {last.cell.label} again"))
        return Decision(Move.BLOCKED, reason="the environment failed twice; fix it, then resume",
                        because=(seen, f"it failed to run {retries} times: past the one retry, a person has to "
                                       "look at the environment"))

    if failure is Failure.TURNS_EXHAUSTED:
        raised = sum(1 for a in attempts if a.failure is Failure.TURNS_EXHAUSTED)
        moving = raised > 1 and bool(last.changed)
        if (raised <= 1 or moving) and last.max_turns < MAX_TURNS_CEILING:
            turns = min(last.max_turns * 2, MAX_TURNS_CEILING)
            rule = (f"a turn ceiling is not a fault of the model or its effort: the same cell gets {turns} turns "
                    f"instead of {last.max_turns}" + (", once" if not moving else "")
                    + f" (the cap is {MAX_TURNS_CEILING})")
            return Decision(Move.MORE_TURNS, last.cell, turns, f"hit {last.max_turns} turns; raised to {turns}",
                            because=(seen, *([f"the raised attempt still moved the work: it changed "
                                              f"{len(last.changed)} file(s), so it is raised again"]
                                             if moving else []), rule))
        if last.max_turns >= MAX_TURNS_CEILING:
            why = f"{last.max_turns} turns is the cap"
        else:
            why = "the turns were raised and the raised attempt changed nothing"
        return Decision(Move.REPLAN, reason="the task does not fit even the raised turn budget; split it",
                        because=(seen, f"{why}: the task is bigger than one worker, so it is split, not climbed"))

    pair = _repeated_pair(attempts)
    if pair and reading:
        # Two cells that only read came back with the same findings: that is the
        # answer, confirmed, not an approach to replace. A stronger model would
        # read the same code and say it a third time, at a higher price.
        first, second = attempts[pair[0] - 1], attempts[pair[1] - 1]
        return Decision(Move.CONVERGED, reason="two cells found the same thing: that is the answer",
                        because=(seen, f"attempts {pair[0]} ({first.cell.label}) and {pair[1]} "
                                       f"({second.cell.label}) of a task that only reads reported the same "
                                       "findings: the answer is confirmed, and climbing would repeat it"))
    if pair or failure is Failure.APPROACH:
        if failure is Failure.APPROACH:
            why = "the approach was wrong"
            rule = ("the worker went the wrong way: more thinking from the same model is the bet that already "
                    "lost, so a different model starts over")
        else:
            why = "two cells gave the same answer"
            first, second = attempts[pair[0] - 1], attempts[pair[1] - 1]
            rule = (f"attempts {pair[0]} ({first.cell.label}) and {pair[1]} ({second.cell.label}) failed the "
                    f"same way ({failure}) and their summaries read alike: two cells agreeing is a settled bet, "
                    "so more effort would give the same answer")
        target = _next_model(ladder, last.cell)
        if target is None:
            return Decision(Move.REPLAN, reason=f"{why}, and there is no stronger model on this ladder",
                            because=(seen, rule, f"no model above {last.cell.model} on this ladder: the task "
                                                 "is rewritten instead"))
        return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval,
                      (seen, rule, _arrives(target)), _skipped(ladder, last.cell, target))

    # Effort axis: the work was shallow. The next effort of the same model,
    # and only when this model has run out does the next one start.
    shallow = "the work happened and was shallow: the same model, more thorough"
    target = _next_effort(ladder, last.cell)
    if target is not None:
        return Decision(Move.MORE_EFFORT, target, last.max_turns, f"{failure}: same model, more thorough",
                        because=(seen, shallow, _arrives(target)))
    target = _next_model(ladder, last.cell)
    if target is None:
        return Decision(Move.EXHAUSTED, reason="the top of the ladder failed too",
                        because=(seen, f"{last.cell.label} is the top of this ladder: there is nothing left "
                                       "to climb to"))
    why = f"{failure}, and {last.cell.label} was this model's last cell"
    return _gated(Move.NEXT_MODEL, target, last.max_turns, why, allow_approval,
                  (seen, shallow, f"{last.cell.label} was {last.cell.model}'s last cell on this ladder, so the "
                                  "next model takes over", _arrives(target)))


def _gated(move: Move, cell: Cell, turns: int, why: str, allow_approval: bool,
           because: tuple[str, ...] = (), skipped: tuple[str, ...] = ()) -> Decision:
    if cell.needs_approval and not allow_approval:
        return Decision(Move.NEEDS_APPROVAL, cell, turns, f"{why}; {cell.label} needs your approval",
                        because=(*because, f"{MODELS[cell.model].alias} runs only when a person says so: "
                                           "resume with --allow-approval, or tag the task #fable"),
                        skipped=skipped)
    return Decision(move, cell, turns, why, because=because, skipped=skipped)
