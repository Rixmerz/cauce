"""Where a lesson belongs: the area of the code it was learned in, the role that
learned it, or everyone.

A worker is shown what failed before only when it is its own: met in an area
its task touches, or by the role it wears. A failure two tasks met in areas
they have none of in common is shared: every worker may meet it again, and the
orchestrating session hears of it too. One met in a single area is that area's: a security worker gets
nothing from how a page's layout broke, and the session that only delegates
needs none of it.

An area is read from paths, no model: rendered UI (`capabilities.UI_FILES`),
tests, docs, and the areas a change can hurt (`risk`: data, security, money,
contract, delivery); a path none of them holds is `code`. A request's areas are
the ones its named paths fall in, and the ones its words are about.

The text a failure is kept and matched by is the work's own: a workflow step's
goal (its run's inputs), never the step's template, which every run of the
workflow shares word for word.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cauce import capabilities, risk
from cauce.store import Store

#: A failure met by this many tasks in cells with no area in common is shared: every
#: worker, and the orchestrator, may meet it.
SHARED_AREAS = 2
#: The area of a path no other area holds.
CODE = "code"
#: Where a shown dead end came from, as the `dead_ends` event keeps it.
SCOPES = ("shared", "area", "role")
#: The meta key the first settle marks once the fixes recorded before scopes have theirs.
BACKFILL_KEY = "cells.backfill"

_TESTS = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$")
_DOCS = re.compile(r"(^|/)docs?/|\.(md|mdx|rst|txt)$")
_UI_TEXT = re.compile(r"\b(ui|ux|page|screen|layout|component|button|modal|css|style\w*|frontend|front-end|"
                      r"island|render\w*|pantalla|vista|bot[oó]n)\b", re.I)


def of_path(path: str) -> str:
    """The one area a path is in: the first that holds it, `code` when none does."""
    for area, patterns in risk.PATH_AREAS:
        if risk.path_matches(path, patterns):
            return area
    if _TESTS.search(path):
        return "tests"
    if _DOCS.search(path):
        return "docs"
    if capabilities.matches(path, capabilities.UI_FILES):
        return "ui"
    return CODE


#: Areas that travel with every change: they name a change's area only when it holds nothing else.
_ALONG = ("tests", "docs")


def of_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """The areas a change is in, sorted: its specific ones (ui, data, security...), else
    `code`, else its tests and docs. A feature's tests and docs go with it and say
    nothing of where it was met."""
    found = {of_path(p) for p in paths if p}
    specific = found - {CODE, *_ALONG}
    return tuple(sorted(specific or (found & {CODE}) or found))


def of_request(text: str, kind: str, paths: Sequence[str] = ()) -> tuple[str, ...]:
    """The areas a request reaches before any work: every one its named paths are in,
    and the ones its words are about."""
    found = {of_path(p) for p in paths if p}
    found.update(area for area, _ in risk.areas(risk.of_request(text, kind)))
    if _UI_TEXT.search(text):
        found.add("ui")
    return tuple(sorted(found))


def memory_text(store: Store, task: Mapping[str, Any]) -> str:
    """What a task's failures are kept and matched by: a workflow step's goal (its run's
    inputs, every value), else the task's own text."""
    run_id = task.get("workflow_run")
    if run_id:
        run = store.get_run(int(run_id))
        if run is not None:
            inputs = json.loads(run.get("inputs") or "{}")
            goal = "\n".join(str(v) for v in inputs.values() if str(v).strip())
            if goal:
                return goal
    return str(task.get("body") or task.get("title") or "")


def areas_of(fix: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(a for a in str(fix.get("areas") or "").split(",") if a)


def scope(problem: Mapping[str, Any], *, role: str | None, areas: Sequence[str]) -> str | None:
    """Whose a problem is for a worker of `role` whose task reaches `areas`: `shared`
    (met by two tasks or more in areas none of them have in common, or written down by
    a person), `area` (met in one of the task's areas), `role` (met by the same role),
    or None: another cell's. One task's change spans what it spans; it is one cell."""
    fixes = problem.get("fixes") or ()
    if any(f.get("task_id") is None for f in fixes) or _across(fixes):
        return "shared"
    met = {a for f in fixes for a in areas_of(f)}
    if met & set(areas):
        return "area"
    if role and any(f.get("role") == role for f in fixes):
        return "role"
    return None


def _across(fixes: Iterable[Mapping[str, Any]]) -> bool:
    """Whether different tasks met a problem in different cells: `SHARED_AREAS` tasks or
    more, with no area all of them were in."""
    by_task: dict[Any, set[str]] = {}
    for fix in fixes:
        if areas_of(fix):
            by_task.setdefault(fix.get("task_id"), set()).update(areas_of(fix))
    if len(by_task) < SHARED_AREAS:
        return False
    return not set.intersection(*by_task.values())


def dead_ends(store: Store, text: str, *, repo: str | None, role: str | None = None,
              areas: Sequence[str] = (), shared_only: bool = False, limit: int = 5) -> list[dict]:
    """The dead ends a worker (or, `shared_only`, the orchestrating session) is shown: a
    strict match of the work's own text, kept to its cell, each with its scope."""
    def keep(problem: Mapping[str, Any]) -> str | None:
        found = scope(problem, role=role, areas=areas)
        return found if not shared_only or found == "shared" else None
    return store.dead_ends(text, repo=repo, limit=limit, strict=True, scope=keep)


def recent_shared(store: Store, *, repo: str | None, limit: int = 5) -> list[dict]:
    """The recent dead ends in a repository that are everyone's: what a session starts with."""
    return store.dead_ends(repo=repo, limit=limit,
                           scope=lambda p: "shared" if scope(p, role=None, areas=()) == "shared" else None)


def backfill(store: Store) -> int:
    """Give the fixes recorded before scopes their role and areas, once: the role their task
    was planned with, the areas of the paths its attempts changed, else of its request.
    Returns how many it scoped."""
    if store.meta(BACKFILL_KEY):
        return 0
    done = 0
    with store.immediate():
        for fix in store.unscoped_fixes():
            task = store.get_task(int(fix["task_id"]))
            if task is None:
                continue
            planned = store.last_event(task["id"], "planned")
            role = ((planned or {}).get("data") or {}).get("role")
            changed = [p for a in store.attempts(task["id"]) for p in json.loads(a.get("changed_paths") or "[]")]
            found = of_paths(changed) or of_request(memory_text(store, task), str(task.get("kind") or ""))
            store.scope_fix(int(fix["id"]), role=role, areas=found)
            done += 1
        store.set_meta(BACKFILL_KEY, str(done))
    return done
