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


def test_every_move_says_what_the_attempt_ended_with_the_rule_and_where_it_goes():
    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.CODE_BUG)])
    assert d.axis == "effort" and d.skipped == ()
    assert d.because[0] == ("attempt 1 at sonnet/medium ended code_bug: the work happened and left a bug or a "
                            "missed edge case")
    assert "shallow" in d.because[1] and d.because[-1] == "next: sonnet/high (the floor for work that needs judgment)"

    d = decide(IMPLEMENT, [fail("sonnet/medium", Failure.APPROACH)])
    assert d.axis == "model" and "the bet that already lost" in d.because[1]
    assert d.because[-1] == "next: opus/high (the floor for work that needs judgment)"
    assert [s.split(":")[0] for s in d.skipped] == ["sonnet/high", "sonnet/xhigh"]

    pair = [fail("sonnet/medium", Failure.CODE_BUG, "the parser still accepts unicode digits"),
            fail("sonnet/high", Failure.CODE_BUG, "the parser still accepts unicode digits.")]
    d = decide(IMPLEMENT, pair)
    assert "attempts 1 (sonnet/medium) and 2 (sonnet/high) failed the same way (code_bug)" in d.because[1]
    assert d.skipped == ("sonnet/xhigh: more effort from sonnet, which is not what failed",)

    d = decide(IMPLEMENT, [fail("sonnet/xhigh", Failure.INCONCLUSIVE)])
    assert "sonnet/xhigh was sonnet's last cell" in d.because[2] and d.because[-1].startswith("next: opus/high")


@pytest.mark.parametrize(
    ("ladder", "attempts", "axis", "phrase"),
    [
        (IMPLEMENT, [fail("sonnet/medium", Failure.ENVIRONMENT)], "retry", "a retry is not an escalation"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.ENVIRONMENT)] * 2, "stop", "failed to run 2 times"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED)], "turns", "60 turns instead of 30"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED)] * 2, "stop", "raised attempt changed nothing"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.TURNS_EXHAUSTED, turns=200)], "stop", "200 turns is the cap"),
        (IMPLEMENT, [fail("sonnet/medium", Failure.SPEC_BUG)], "stop", "rewritten or split"),
        (IMPLEMENT, [Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "", ("Bash(make)",))],
         "stop", "refused: Bash(make)"),
        (IMPLEMENT, [fail("opus/high", Failure.CODE_BUG)], "stop", "the top of this ladder"),
        (IMPLEMENT, [fail("opus/high", Failure.APPROACH)], "stop", "no model above opus"),
        (PLAN, [fail("opus/max", Failure.CODE_BUG)], "stop", "resume with --allow-approval"),
    ],
)
def test_moves_that_stop_or_stay_explain_themselves(ladder, attempts, axis, phrase):
    d = decide(ladder, attempts)
    assert d.axis == axis and d.because[0].startswith(f"attempt {len(attempts)} at ")
    assert any(phrase in line for line in d.because), d.because


def test_a_raised_attempt_that_still_moved_the_work_is_raised_again_up_to_the_cap():
    """A large removal used all its turns twice while changing dozens of files each
    time; it was sent back to be split when the work was nearly done."""
    def ran_out(turns, changed=()):
        return Attempt(Cell("sonnet", "medium"), turns, False, Failure.TURNS_EXHAUSTED, "", changed=changed)

    d = decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60, ("b.ts", "c.ts"))])
    assert d.move is Move.MORE_TURNS and d.max_turns == 120
    assert "changed 2 file(s), so it is raised again" in d.because[1] and "once" not in d.because[2]
    d = decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60, ("b.ts",)), ran_out(120, ("c.ts",))])
    assert d.move is Move.MORE_TURNS and d.max_turns == 200
    d = decide(IMPLEMENT, [ran_out(30), ran_out(60), ran_out(120), ran_out(200, ("d.ts",))])
    assert d.move is Move.REPLAN and "200 turns is the cap" in d.because[1]
    assert decide(IMPLEMENT, [ran_out(30, ("a.ts",)), ran_out(60)]).move is Move.REPLAN


def test_a_refusal_names_the_rules_that_let_it_through():
    refused = Attempt(Cell("sonnet", "medium"), 30, False, Failure.PERMISSION, "",
                      ("Bash(tg -n x src; npx tsc --noEmit)",), allow=("Bash(tg:*)", "Bash(npx tsc:*)"))
    d = decide(IMPLEMENT, [refused])
    assert "allow it (--allow 'Bash(tg:*)' --allow 'Bash(npx tsc:*)'), then resume" in d.reason
    assert d.because[-1] == "the rules that let it through, one per program: Bash(tg:*), Bash(npx tsc:*)"


def test_an_attempt_that_was_refused_blocks_whatever_else_it_ended_with():
    """A worker refused its tools, then its check went red: that was read as shallow
    work and climbed to more effort, which met the same refusals."""
    refused = Attempt(Cell("opus", "high"), 30, False, Failure.CODE_BUG, "tests failed", ("Bash(node x.js)",),
                      allow=("Bash(node:*)",))
    d = decide(LADDERS["debug-unclear"], [refused])
    assert d.move is Move.BLOCKED and "--allow 'Bash(node:*)'" in d.reason
    wrong = Attempt(Cell("opus", "high"), 30, False, Failure.SPEC_BUG, "", ("Bash(x)",))
    assert decide(LADDERS["debug-unclear"], [wrong]).move is Move.REPLAN  # a wrong task is still wrong


def test_a_reading_task_whose_cells_agree_has_its_answer():
    """An audit found the same stray references at three cells and climbed to the top
    of its ladder; finding them was the job, done at the first cell."""
    said = "the API still has 'admin' in the baseline migration and the seed script"
    attempts = [fail("opus/high", Failure.CODE_BUG, said), fail("opus/xhigh", Failure.CODE_BUG, said + ".")]
    d = decide(LADDERS["review-critical"], attempts, reading=True)
    assert d.move is Move.CONVERGED and not d.continues and d.axis == "stop"
    assert "the answer is confirmed" in d.because[1]
    # a task that writes still changes model on a repeated answer
    assert decide(LADDERS["review-critical"], attempts).move is Move.REPLAN  # no stronger model left
