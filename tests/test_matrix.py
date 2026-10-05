from __future__ import annotations

import pytest

from cauce.matrix import EFFORTS, KINDS, LADDERS, MODELS, READ_ONLY_KINDS, Cell, estimate_usd, ladder_for


def test_every_effort_level_exists_and_haiku_has_none():
    assert EFFORTS == ("low", "medium", "high", "xhigh", "max")
    assert Cell("haiku").label == "haiku"
    with pytest.raises(ValueError):
        Cell("haiku", "low")
    with pytest.raises(ValueError):
        Cell("sonnet")
    with pytest.raises(ValueError):
        Cell("gpt", "high")


def test_parse_round_trips():
    for label in ("haiku", "sonnet/low", "opus/xhigh", "fable/max"):
        assert Cell.parse(label).label == label


def test_ladders_climb_and_never_go_down():
    """Within a ladder, each cell is a stronger model or the same model with more effort."""
    for kind, ladder in LADDERS.items():
        keys = [c.sort_key() for c in ladder]
        assert keys == sorted(keys), kind
        assert len(set(ladder)) == len(ladder), kind


def test_every_effort_level_is_used_somewhere():
    used = {c.effort for ladder in LADDERS.values() for c in ladder}
    assert set(EFFORTS) <= used


def test_fable_needs_approval_and_only_planning_or_a_tag_reaches_it():
    assert Cell("fable", "high").needs_approval
    reaches = {k for k, ladder in LADDERS.items() if any(c.model == "fable" for c in ladder)}
    assert reaches == {"plan", "frontier"}


def test_sort_key_is_not_alphabetical():
    assert Cell("haiku").sort_key() < Cell("sonnet", "low").sort_key() < Cell("opus", "low").sort_key()
    assert Cell("opus", "max").sort_key() < Cell("fable", "low").sort_key()


def test_unknown_kind_falls_back_and_read_only_kinds_are_known():
    assert ladder_for("nope") == LADDERS["implement"]
    assert set(READ_ONLY_KINDS) <= set(KINDS)


def test_estimate():
    assert estimate_usd(Cell("sonnet", "low"), 1_000_000, 100_000) == pytest.approx(3.0)
    assert set(MODELS) == {"haiku", "sonnet", "opus", "fable"}
