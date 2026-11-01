"""Branches of `locks.standards` the standards suite leaves unread."""
from __future__ import annotations

import contextlib
from datetime import timedelta

from locks import standards
from locks.store import Store

from .test_standards import NOW, WASTED, finished, many, skips_of


def _climbs(store: Store, *tries) -> list[standards.Climb]:
    task_id = finished(store, *tries)
    (task,) = [t for t in standards.ended(store) if t.id == task_id]
    return standards.climbs(task)


def test_no_repository_has_no_size_ratio_and_no_skips(store: Store):
    assert standards.size_ratio(store, None, "implement") == (None, 0)
    assert standards.active_skips(store, None, "implement") == {}


def test_a_climb_whose_tries_on_the_new_rung_said_nothing_is_no_climb(store: Store):
    found = _climbs(store, ("sonnet/high", False, "code_bug", "more_effort"),
                    ("sonnet/xhigh", False, "environment", "retry"))
    assert found == []


def test_a_climb_is_read_up_to_the_try_that_left_its_rung(store: Store):
    found = _climbs(store, ("sonnet/high", False, "code_bug", "more_effort"),
                    ("sonnet/xhigh", False, "code_bug", "retry"),
                    ("opus/high", True))
    assert [(c.move, c.source, c.target, c.passed) for c in found] == [
        ("more_effort", "sonnet/high", "sonnet/xhigh", False)]


def test_a_climb_off_the_ladder_has_no_rung_to_skip_onto():
    assert standards.skip_onto(standards.ladder_for("implement"), "haiku/low", "sonnet/high") is None


def test_a_skip_whose_rung_left_the_ladder_stops(store: Store):
    store.add_calibration(at=standards._stamp(NOW), repo="r", kind="implement", band=standards.ANY_SIZE,
                          field=standards.SKIP, old_value=None, new_value="sonnet/xhigh>opus/high", rule="old",
                          evidence={}, version="t")
    assert standards.active_skips(store, "r", "implement") == {}


def test_two_climbs_too_few_here_both_read_every_repository(store: Store):
    # The climbs past sonnet/high pass (no skip there), the ones into it never do: both read
    # every repository, the second from what the first already read.
    tries = (("sonnet/medium", False, "code_bug", "more_effort"), ("sonnet/high", False, "code_bug", "more_effort"),
             ("sonnet/xhigh", True))
    finished(store, *tries, repo="r")
    many(store, standards.SKIP_MIN_MOVES, *tries, repo="o")
    [change] = skips_of(standards.calibrate_skips(store, "r", now=NOW))
    assert change["new_value"] == "sonnet/medium>sonnet/high" and change["evidence"]["pooled"]


def test_a_skip_with_too_few_climbs_since_waits_for_its_verdict(store: Store):
    many(store, standards.SKIP_MIN_MOVES, *WASTED)
    [change] = skips_of(standards.calibrate(store, "r", now=NOW))
    assert standards.judge(store, "r", now=NOW + timedelta(minutes=1)) == []
    assert change["id"] not in store.verdicts()


def test_a_backfill_another_settle_made_meanwhile_is_not_made_again(store: Store, monkeypatch):
    finished(store, ("sonnet/medium", True))
    real = store.immediate

    @contextlib.contextmanager
    def raced():
        store.set_meta(standards.BACKFILL_KEY, '{"by": "the other settle"}')
        with real():
            yield

    monkeypatch.setattr(store, "immediate", raced)
    standards.settle(store, now=NOW)
    assert store.meta(standards.BACKFILL_KEY) == '{"by": "the other settle"}'
    assert store.meta("standards:since:r") != "0"
