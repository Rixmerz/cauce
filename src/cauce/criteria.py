"""What was asked, kept as asked: a request's items as numbered criteria.

A request that lists what it wants (bullets, numbered lines) becomes criteria
`S1..Sn`, each the person's own words, verbatim. Every worker and every check
reads the same words: a paraphrase loses the detail that decides a check. A
worker that claims a pass says, criterion by criterion, whether each is met
and what shows it; a pass that leaves one out, or says one is not met, is not
a pass.

A request in prose has no criteria: splitting sentences would invent
structure the person did not write.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)]|\[[ xX]\])\s+(?P<text>\S.*)$")
MAX_ITEMS = 20
ITEM_CHARS = 400


def items(text: str) -> list[str]:
    """The request's listed items, verbatim, when it lists two or more."""
    found = []
    for line in text.splitlines():
        match = _ITEM.match(line)
        if match:
            found.append(match.group("text").strip()[:ITEM_CHARS])
    return found[:MAX_ITEMS] if len(found) >= 2 else []


def ids(text: str) -> tuple[str, ...]:
    return tuple(f"S{i}" for i, _ in enumerate(items(text), 1))


def brief(text: str) -> str:
    """The criteria for the brief, or "" when the request lists none."""
    found = items(text)
    if not found:
        return ""
    lines = "\n".join(f"S{i}: {item}" for i, item in enumerate(found, 1))
    return ("The request's criteria, in the person's words. Check each one; a pass says for every one whether "
            "it is met and what shows it, in `criteria`:\n" + lines)


def read(block: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The criteria a result block reports: id, met, evidence; strings and booleans only."""
    raw = block.get("criteria")
    if not isinstance(raw, list):
        return ()
    out = []
    for c in raw:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("met"), bool):
            out.append({"id": c["id"].strip().upper(), "met": c["met"],
                        "evidence": str(c.get("evidence") or "")[:500]})
    return tuple(out)


def unmet(expected: Sequence[str], reported: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """The criteria a pass leaves out, and the ones it says are not met."""
    said = {c["id"]: c["met"] for c in reported}
    return [i for i in expected if i not in said], [i for i in expected if said.get(i) is False]
