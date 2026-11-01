"""Two arms, tried in turns until each has enough first tries, then the one that did no worse.

The shape every measured choice in cauce shares (a method, the order of a brief's notes): tasks
alternate between an arm `with` the change and one without it, the arm with fewer tasks next,
until each holds `trial` first tries; then the change stays only where its arm passed its first
tries no less often. No model, no clock: the record of first tries decides.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def tally(rows: Iterable[Mapping[str, Any]], arms: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per arm: its tasks, first tries and the ones that passed, from rows of (`arm`, `tries`, `passed`)."""
    out: dict[str, dict[str, Any]] = {a: {"tasks": 0, "tries": 0, "passed": 0} for a in arms}
    for row in rows:
        side = out.get(str(row["arm"]))
        if side is None:
            continue
        side["tasks"] += 1
        side["tries"] += int(row["tries"])
        side["passed"] += int(row["passed"])
    for side in out.values():
        side["rate"] = round(side["passed"] / side["tries"], 3) if side["tries"] else None
    return out


def pick(found: Mapping[str, Mapping[str, Any]], arms: Sequence[str], task_id: int, trial: int,
         name: str) -> tuple[str, str]:
    """The arm this task goes in, and why: `arms[0]` is the change, `arms[1]` its absence."""
    on, off = arms
    w, wo = found[on], found[off]
    if min(w["tries"], wo["tries"]) < trial:
        side = arms[task_id % 2] if w["tasks"] == wo["tasks"] else on if w["tasks"] < wo["tasks"] else off
        return side, f"the {name} trial here ({w['tries']} {on}, {wo['tries']} {off}, {trial} each)"
    said = f"first tries passed {w['rate']:.0%} {on} and {wo['rate']:.0%} {off}"
    if w["rate"] >= wo["rate"]:
        return on, f"the {name} won its trial here: {said}"
    return off, f"the {name} lost its trial here: {said}"
