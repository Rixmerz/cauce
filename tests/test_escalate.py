from __future__ import annotations

import pytest

from cauce.escalate import Attempt, Failure, Move, decide, repeated_answer
from cauce.matrix import LADDERS, Cell

IMPLEMENT = LADDERS["implement"]  # sonnet/medium, sonnet/high, sonnet/xhigh, opus/high
PLAN = LADDERS["plan"]  # opus/xhigh, opus/max, fable/high


def fail(cell: str, failure: Failure, summary: str = "", turns: int = 30) -> Attempt:
    return Attempt(Cell.parse(cell), turns, False, failure, summary)


def test_shallow_work_climbs_effort_on_the_same_model():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG)])
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "high")


def test_when_the_model_has_no_more_effort_the_next_model_starts():
    d = decide(IMPLEMENT, [fail("sonnet/xhigh", Failure.CODE_BUG)])
    assert d.move is Move.NEXT_MODEL and d.cell == Cell("opus", "high")


def test_a_wrong_approach_jumps_model_without_spending_effort_rungs():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.APPROACH)])
    assert d.move is Move.NEXT_MODEL and d.cell == Cell("opus", "high")


def test_two_cells_saying_the_same_thing_jump_model():
    attempts = [
        fail("sonnet/medium", Failure.CODE_BUG, "the parser still accepts unicode digits"),
        fail("sonnet/high", Failure.CODE_BUG, "the parser still accepts unicode digits."),
    ]
    assert repeated_answer(attempts)
    assert decide(IMPLEMENT, attempts).move is Move.NEXT_MODEL


def test_the_same_cell_agreeing_with_itself_is_not_a_repeat():
    attempts = [fail("sonnet/high", Failure.CODE_BUG, "same"), fail("sonnet/high", Failure.CODE_BUG, "same")]
    assert not repeated_answer(attempts)
    empty = [fail("sonnet/medium", Failure.CODE_BUG, ""), fail("sonnet/high", Failure.CODE_BUG, "")]
    assert not repeated_answer(empty)


def test_environment_failures_retry_once_then_block():
    first = decide(IMPLEMENT, [fail("sonnet/medium", Failure.ENVIRONMENT)])
    assert first.move is Move.RETRY and first.cell == Cell("sonnet", "medium")
    second = decide(IMPLEMENT, [fail("sonnet/medium", Failure.ENVIRONMENT)] * 2)
    assert second.move is Move.BLOCKED


def test_turn_ceiling_is_raised_once_then_the_task_is_split():
    first = decide(IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=30)])
    assert first.move is Move.MORE_TURNS and first.max_turns == 60 and first.cell == Cell("sonnet", "medium")
    second = decide(IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=30),
                                fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=60)])
    assert second.move is Move.REPLAN


@pytest.mark.parametrize("failure", [Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG])
def test_a_wrong_task_replans(failure):
    assert decide(IMPLEMENT, [fail("sonnet/medium", failure)]).move is Move.REPLAN


def test_the_top_of_the_ladder_is_the_end():
    assert decide(IMPLEMENT, [fail("opus/high", Failure.CODE_BUG)]).move is Move.EXHAUSTED
    assert decide(IMPLEMENT, [fail("opus/high", Failure.APPROACH)]).move is Move.REPLAN


def test_fable_waits_for_a_person():
    d = decide(PLAN, [fail("opus/max", Failure.CODE_BUG)])
    assert d.move is Move.NEEDS_APPROVAL and d.cell == Cell("fable", "high")
    assert decide(PLAN, [fail("opus/max", Failure.CODE_BUG)], allow_approval=True).move is Move.NEXT_MODEL


def test_a_cell_off_the_ladder_still_climbs():
    d = decide(IMPLEMENT, [fail("sonnet/low", Failure.CODE_BUG)])
    assert d.move is Move.MORE_EFFORT and d.cell == Cell("sonnet", "medium")
    d = decide(IMPLEMENT, [fail("opus/low", Failure.CODE_BUG)])
    assert d.cell == Cell("opus", "high")


def test_decide_needs_a_failure():
    with pytest.raises(ValueError):
        decide(IMPLEMENT, [])
    with pytest.raises(ValueError):
        decide(IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, True)])
    assert decide(IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, False)]).move is Move.MORE_EFFORT


def test_a_refused_command_blocks_at_once_and_names_what_to_allow():
    refused = Attempt(Cell("haiku"), 30, False, Failure.PERMISSION, "build refused",
                      ("Bash(npm run build)", "Bash(node server.js)", "Bash(ls)", "Read(x)", "Read(y)"))
    d = decide(LADDERS["implement"], [refused])
    assert d.move is Move.BLOCKED and not d.continues
    assert "Bash(npm run build), Bash(node server.js), Bash(ls) and 2 more" in d.reason and "--allow" in d.reason
    assert "a tool call" in decide(LADDERS["implement"], [Attempt(Cell("haiku"), 30, False, Failure.PERMISSION)]).reason
