"""A race neither side won is counted as one, and the rung's try is a first try of its own."""
from __future__ import annotations

from locks import racing, standards


def _row(cell, passed, failure=None, move=None):
    return {"cell": cell, "passed": int(passed), "failure": failure, "move": move}


def test_a_race_neither_side_won_is_read_from_the_rung_s_mark():
    """The start's row takes the next move (it went on), so the race is marked on the rung's: the
    `race` mark on the start's was overwritten and racing never counted such a race."""
    rows = [_row("sonnet/medium", False, "code_bug", "more_effort"),
            _row("sonnet/high", False, "environment", racing.RACED_BESIDE),
            _row("sonnet/high", True)]
    assert racing.outcome(rows) == racing.NEITHER
    # The other outcomes read as before, and an older record with `race` on the start's too.
    assert racing.outcome([_row("a/b", False, move=racing.RACED_OUT), _row("c/d", True)]) == racing.RUNG
    assert racing.outcome([_row("a/b", True, move="race"), _row("c/d", False, move=racing.RACED_OUT)]) == racing.START
    assert racing.outcome([_row("a/b", False, move="race"), _row("c/d", False)]) == racing.NEITHER
    assert racing.outcome([_row("a/b", False, move="retry"), _row("a/b", True)]) is None


def test_the_rung_s_try_is_a_first_try_and_the_start_s_goes_on_after_it():
    rows = [_row("sonnet/medium", False, "code_bug", "more_turns"),
            _row("sonnet/high", False, "code_bug", racing.RACED_BESIDE),
            _row("sonnet/medium", True)]
    # The start's try went on with more turns and passed; the rung's failed on the work.
    assert standards.samples(rows) == [("sonnet/medium", True), ("sonnet/high", False)]
    # A rung only the environment stopped says nothing of its cell: no sample.
    rows[1] = _row("sonnet/high", False, "environment", racing.RACED_BESIDE)
    assert standards.samples(rows) == [("sonnet/medium", True)]


def test_a_real_race_neither_won_is_marked_on_the_rung(git_repo, store, monkeypatch):
    from .test_cover_engine import (
        test_orchestrate_a_race_whose_rung_only_met_the_environment_goes_on_from_the_start as run_it,
    )

    run_it(git_repo, store, monkeypatch)
    task = store.list_tasks(limit=1)[0]
    rows = store.attempts(task["id"])
    assert rows[1]["move"] == racing.RACED_BESIDE and racing.outcome(rows) == racing.NEITHER
