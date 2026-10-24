"""The loop: classify, choose a starting cell, attempt, check, move, remember.

Everything here is deterministic code. The models are called for two things
only — to classify a request the rules did not recognise, and to do the work —
and nothing a model says about its own work decides the next move by itself:
the result block is checked for evidence, an optional verify command has the
last word on a claimed pass, and the failure kind is read against the ladder by
`escalate.decide`.

What a report says changed is git's account, printed beside the worker's own:
a person reading "I created the routes" can see on the next line whether any
file says so.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cauce import (
    after,
    cells,
    ci,
    config,
    dispatch,
    grants,
    habits,
    isolate,
    launch,
    models,
    notes,
    plugins,
    project,
    racing,
    repo,
    revisions,
    roles,
    runtime,
    standards,
    stops,
    teams,
    workflows,
)
from cauce import allow as allow_rules
from cauce import capabilities as caps
from cauce import criteria as criteria_mod
from cauce import findings as findings_mod
from cauce import ledger as ledger_mod
from cauce import points as points_mod
from cauce import risk as risk_mod
from cauce import services as services_mod
from cauce import surfaces as surfaces_mod
from cauce.adapters import ABSENT, Adapter, Status, default_adapters
from cauce.classify import Classification, classify, narrow
from cauce.escalate import (
    MAX_TURNS_CEILING,
    REPLAN,
    Attempt,
    Decision,
    Failure,
    Move,
    decide,
    review_reason,
    waited,
)
from cauce.matrix import DEFAULT_TURNS, READ_ONLY_KINDS, Cell, ladder_for, turns_for
from cauce.store import Store, home
from cauce.text import clip

#: Failures that describe something tried against the problem, worth keeping
#: as a dead end. A timeout or a turn ceiling says nothing about the fix.
_MEMORABLE = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.APPROACH, Failure.SPEC_BUG,
                        Failure.ARCHITECTURE_BUG})

#: How a brief names where a dead end came from (`cauce.cells.scope`).
_SCOPE_SAID = {"shared": "met across areas", "area": "met in this task's area", "role": "met by this role"}


class Cancelled(Exception):
    """Raised into a run when someone cancels it; the run stops and cleans up."""


_FINAL_STATUS = {
    Move.REPLAN: "replan",
    Move.NEEDS_APPROVAL: "needs_approval",
    Move.EXHAUSTED: "failed",
    Move.BLOCKED: "blocked",
    Move.CONVERGED: "failed",
    Move.REVIEW: "blocked",
    Move.DECIDE: "blocked",
}

#: Endings that wait on a person, not on the task being rewritten: the work done
#: so far is kept on the task's branch, unverified, for them to see and resume.
KEEPS_WORK = frozenset({"done", "blocked", "needs_approval"})

#: How many finished tasks of a kind it takes before history moves the start.
MIN_REPO_SAMPLES = 3
MIN_GLOBAL_SAMPLES = 5
#: How many recent tasks of a kind here that outgrew their turns raise where the next one's start,
#: and how many recent tasks are looked at.
MIN_TURN_SAMPLES, TURN_HISTORY = 2, 10
VERIFY_TIMEOUT_S = 900
#: How many changed paths a task's `finished` event lists; `changed_n` counts them all.
CHANGED_KEPT = 500

Launcher = Callable[[launch.LaunchSpec], launch.WorkerResult]


@dataclass
class Options:
    max_attempts: int = 6
    allow_approval: bool = False
    isolate: bool = True
    verify: str | None = None
    launch_dir: Path | None = None
    #: None: the kind's own (`matrix.turns_for`).
    max_turns: int | None = None
    use_model_classifier: bool = True
    kind: str | None = None
    start: Cell | None = None
    dry_run: bool = False
    #: None reads the `livespec` setting (on by default).
    livespec: bool | None = None
    #: Permission rules a person granted the workers, e.g. `Bash(npm run build)`.
    allow_tools: tuple[str, ...] = ()
    #: None reads the `notes` setting (on by default): the project's notes in the
    #: brief, and what a passing worker learned filed into them.
    notes: bool | None = None
    #: The commit a new worktree starts from instead of HEAD: a workflow step
    #: builds on the work of the step before it.
    base: str | None = None
    #: A role a person named for this run: it runs as that role, on the kinds it takes.
    role: str | None = None
    #: Where the task may write (`cauce.surfaces`): a change outside is undone and fails the attempt.
    surfaces: tuple[str, ...] = ()
    #: The role a workflow step names: used when it is in the project's team,
    #: since a definition a repository commits must not choose beyond the person's team.
    step_role: str | None = None
    #: A workflow step's part in a review ledger (`cauce.ledger`): its role, round and rows.
    ledger: dict | None = None
    #: What a passing worker learned is filed in the project's notes (and engram, when
    #: writing there is on). Off for a task whose findings wait on a person's agreement.
    keep_learned: bool = True
    #: Bring the person's uncommitted changes into a new worktree: work the task
    #: builds on that no commit holds yet.
    carry: bool = False
    #: Directories beside the work a worker may read and use (a sibling repository).
    add_dirs: tuple[str, ...] = ()
    #: Servers the task needs up, `command@port`, started and stopped by cauce
    #: around the run (`cauce.services`): the person's flag, never a file's.
    services: tuple[str, ...] = ()
    #: A workflow run's check, for a step classified when it runs: it becomes the
    #: step's `verify` only when the step turns out to write.
    run_verify: str | None = None
    #: A plan step a workflow fans out over: its pass carries the parts and their contract.
    parts: bool = False
    #: A workflow's integrate step: the parts' branches it joins, in the plan's order
    #: (`{part, task, branch}` each), and the contract they keep.
    integrate: dict | None = None
    #: The files the change a step reads touches (a review's diff): capabilities about
    #: them join it (`Capability.touches`).
    touches: tuple[str, ...] = ()
    #: A workflow step a blind review of its change waits on: that review reads the
    #: request and the diff as the independent verifier would, so the verifier is not
    #: run twice on the same work (`workflows.reviewed_next`).
    reviewed_next: bool = False
    #: The session's word that the task is one piece (`--whole`): out of turns, it gets more.
    whole: bool = False

    def stored(self) -> dict:
        """What a resumed run is started with again, as the task's `options`. A run
        in the checkout itself resumes there: in a worktree it could not see the
        uncommitted work it was continuing."""
        values = {"verify": self.verify, "kind": self.kind,
                  "allow_approval": self.allow_approval, "allow_tools": list(self.allow_tools),
                  "livespec": self.livespec, "no_isolate": not self.isolate, "role": self.role,
                  "step_role": self.step_role, "surfaces": list(self.surfaces),
                  "ledger": self.ledger, "add_dirs": list(self.add_dirs), "services": list(self.services),
                  "run_verify": self.run_verify, "parts": self.parts, "integrate": self.integrate,
                  "touches": list(self.touches), "reviewed_next": self.reviewed_next, "whole": self.whole,
                  # What `flow.queued_options` reads back: a resume stored without them lost them
                  # for every resume after it (a step's start cell, its turns, its base commit).
                  "start": self.start.label if self.start else None, "max_turns": self.max_turns,
                  "base": self.base, "carry": self.carry,
                  "max_attempts": self.max_attempts if self.max_attempts != Options.max_attempts else None}
        kept = {k: v for k, v in values.items() if v not in (None, [], False)}
        if not self.keep_learned:
            kept["keep_learned"] = False  # a task whose findings wait on a person's word
        if self.livespec is False:
            kept["livespec"] = False  # a person's --no-livespec, not the setting's default
        return kept


@dataclass
class Plan:
    kind: str
    classification: Classification
    ladder: tuple[Cell, ...]
    start: Cell
    reasons: list[str]
    capabilities: tuple[str, ...]
    #: What adopted neighbours said before the first attempt, for the brief.
    context: tuple[str, ...] = ()
    neighbours: tuple[Status, ...] = ()
    #: The adopted neighbours that are present, with their status.
    active: tuple[tuple[Adapter, Status], ...] = ()
    #: The project's notes the workers are shown.
    notes: tuple[dict, ...] = ()
    #: The role the workers run as, and the team it came from.
    role: roles.Role | None = None
    team: str = ""
    role_why: str = ""
    #: How much the change can hurt, from the request; attempts may raise it.
    risk: risk_mod.Risk = field(default_factory=risk_mod.Risk)
    #: The turns the first attempt starts with when history raised them; None: the kind's own.
    turns: int | None = None
    #: The task's size on the Fibonacci scale, from what the plan knows (`cauce.points`).
    points: points_mod.Score = field(default_factory=lambda: points_mod.Score(1))
    #: What it took from the standard of its band (`cauce.standards`): None outside a repository.
    standard: dict | None = None
    #: The rung its first try races beside the start (`race_cell`): None when it does not.
    race: Cell | None = None


@dataclass(frozen=True)
class WorkerSession:
    """The last attempt's Claude Code session, as the next attempt may continue it."""
    #: The session id; "" when none was kept (not in a worktree, or the CLI gave none).
    id: str
    #: What ran it: the model id a person pinned, else the alias.
    model: str
    cell: Cell
    attempt: int


@dataclass
class Report:
    task_id: int | None
    status: str
    plan: Plan
    cells: list[str] = field(default_factory=list)
    #: The model that served each attempt, as the CLI reported it ("" when it did not).
    served: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    #: Each move's full account: where it went, along which dial, and why.
    decisions: list[Decision] = field(default_factory=list)
    final_cell: str | None = None
    cost_usd: float = 0.0
    branch: str | None = None
    summary: str = ""
    impact: list[str] = field(default_factory=list)
    #: What the task changed, from git — never from the worker.
    changed: list[str] = field(default_factory=list)
    #: Files cauce's own check wrote in the worktree (`isolate.rewritten`): committed as its output, and said.
    check_output: list[str] = field(default_factory=list)
    #: Whether it worked in a worktree; outside one, its changes are in the checkout.
    isolated: bool = False
    #: What checked the work it left: "cauce" (its check, or `--verify`, passed), "worker"
    #: (only the worker's own check, not re-run by cauce), or "" (nothing passed).
    checked: str = ""
    #: The commit its kept branch ends on.
    commit: str | None = None
    #: Why it stopped short of a pass, and who made that call.
    stop: stops.Stop | None = None
    #: Earlier copies of this task, same text, taken off the board by its pass.
    superseded: list[int] = field(default_factory=list)
    #: What the passing worker answered for each criterion `S#` of the request: id, met, evidence.
    criteria: tuple[dict[str, Any], ...] = ()
    #: What the passing worker learned, and where each fact was filed in the notes.
    learned: tuple[str, ...] = ()
    noted: list[dict] = field(default_factory=list)
    #: A passing plan's parts and the contract they keep, for the workflow step that fans out over it.
    parts: tuple[dict, ...] = ()
    contract: str = ""
    #: Security problems the passing worker named (`alerts`), secrets masked.
    alerts: list[str] = field(default_factory=list)
    #: cauce's check failed only as it does at the base (`Baseline.found`): what it found, else None.
    preexisting: dict[str, Any] | None = None

    def checked_text(self) -> str:
        """What checked the work, said as it is: "unverified" only when no check cauce ran passed."""
        if self.checked == "cauce":
            return "checked by cauce's own run of its check"
        if self.checked == "worker":
            return "checked by the worker's own check, not re-run by cauce"
        return "unverified"

    def text(self) -> str:
        lines = [
            f"task #{self.task_id}: {self.status}" if self.task_id else f"plan: {self.status}",
            f"kind {self.plan.kind} ({self.plan.classification.source}: {self.plan.classification.reason})",
            f"ladder {' → '.join(c.label for c in self.plan.ladder)}",
            f"start {self.plan.start.label}" + "".join(f"\n  · {r}" for r in self.plan.reasons),
        ]
        if self.plan.role is not None:
            lines.append(f"role {self.plan.role.name} ({self.plan.role.scope}) of the {self.plan.team} team: "
                         f"{self.plan.role_why}")
        lines.append(f"risk {self.plan.risk.tier}" + (f": {'; '.join(self.plan.risk.reasons)}"
                                                      if self.plan.risk.reasons else ""))
        lines.append(f"points {self.plan.points.line()}")
        lines += [status.line() for status in self.plan.neighbours]
        if self.task_id is None:
            # A dry run shows what the first worker would be handed.
            lines += self.plan.context
        if self.plan.capabilities:
            lines.append(f"capabilities {', '.join(self.plan.capabilities)}")
        for i, (cell, move) in enumerate(zip(self.cells, [*self.moves, ""], strict=False), 1):
            model = self.served[i - 1] if i <= len(self.served) and self.served[i - 1] else ""
            lines.append(f"attempt {i} {cell}" + (f" [{model}]" if model else "") + (f" → {move}" if move else ""))
            if i <= len(self.decisions):
                d = self.decisions[i - 1]
                lines += [f"    · {line}" for line in d.because[1:]]
                lines += [f"    · skipped {line}" for line in d.skipped]
        if self.final_cell:
            lines.append(f"passed at {self.final_cell}")
        if self.cost_usd:
            lines.append(f"cost ${self.cost_usd:.2f}")
        if self.task_id and self.plan.kind not in READ_ONLY_KINDS:
            more = len(self.changed) - 8
            shown = ", ".join(self.changed[:8]) + (f" and {more} more" if more > 0 else "")
            lines.append(f"changed (from git): {shown or 'nothing'}")
            if self.check_output:
                more = len(self.check_output) - 8
                wrote = ", ".join(self.check_output[:8]) + (f" and {more} more" if more > 0 else "")
                lines.append(f"written by the check itself ({len(self.check_output)}, committed as its output; "
                             f"a check that rewrites tracked files may give other files in your checkout): {wrote}")
        at = f" (commit {self.commit[:12]})" if self.commit else ""
        if self.branch and self.status == "cancelled":
            lines.append(f"branch {self.branch} keeps what it did before the cancel, unverified: "
                         f"`git diff HEAD...{self.branch}` shows it; `git branch -D {self.branch}` drops it")
        elif self.branch and self.status == "done":
            lines.append(f"branch {self.branch}{at} — review it, then merge it; your checkout has none of it until "
                         f"then; {self.checked_text()}")
        elif self.branch:
            then = ("decide the open point, then merge it" if self.stop is not None and self.stop.cause == "decision"
                    else f"or `cauce resume {self.task_id}` once the block is cleared")
            lines.append(f"branch {self.branch}{at} keeps this task's work, {self.checked_text()}: review it with "
                         f"`git diff HEAD...{self.branch}`, {then}")
        elif self.task_id and self.changed and not self.isolated:
            # No worktree (asked for, or a folder that is no checkout): there is no
            # branch to review, the files themselves are the work.
            lines.append(f"these changes are in your checkout (the run had no worktree), {self.checked_text()}")
        elif self.task_id and self.status != "done" and self.changed:
            lines.append("nothing of it was kept")
        if said := preexisting_line(self.preexisting):
            lines.append(said)
        lines += self.impact
        if self.superseded:
            lines.append("off the board: " + ", ".join(f"#{i}" for i in self.superseded)
                         + ", earlier copies of this task that had stopped")
        lines += [f"⚠ security: {alert}" for alert in self.alerts]
        if self.noted:
            lines.append("notes: " + "; ".join(
                f"{'kept' if n['new'] else 'already known as'} #{n['id']} [{n['topic']}] {n['title']}"
                for n in self.noted))
        if self.summary:
            lines.append(self.summary)
        if self.stop is not None and self.task_id:
            lines.append(f"stopped by {stops.WHO.get(self.stop.who, self.stop.who)}: {stops.what_to_do(self.stop)}")
            command = stops.next_step(self.task_id, self.stop)
            if command:
                lines.append(f"  {command}")
            rules = self.stop.allow or allow_rules.from_refusals(self.stop.denied)
            if self.stop.cause == "permission" and rules:
                lines.append("  or, for every task in this repository: cauce allow "
                             + " ".join(shlex.quote(r) for r in rules))
        return "\n".join(lines)


def choose_start(
    ladder: Sequence[Cell],
    complexity: str,
    repo_landings: Sequence[str],
    global_landings: Sequence[str],
) -> tuple[Cell, list[str]]:
    """The cell the first attempt runs at, and why.

    History only ever *raises* the start. A kind that always lands two rungs up
    in this repository starts there and skips two failed attempts; a kind that
    always passes first try already starts at the bottom.
    """
    index, reasons = 0, []
    if complexity == "high" and len(ladder) > 1:
        index = 1
        reasons.append("classified as high complexity: one rung up")
    samples, where = (repo_landings, "this repository") if len(repo_landings) >= MIN_REPO_SAMPLES else (
        global_landings, "all repositories")
    if len(samples) >= (MIN_REPO_SAMPLES if where == "this repository" else MIN_GLOBAL_SAMPLES):
        positions = sorted(_index_of(ladder, Cell.parse(label)) for label in samples)
        learned = positions[(len(positions) - 1) // 2]
        if learned > index:
            index = learned
            reasons.append(f"in {where}, half of the last {len(samples)} tasks of this kind "
                           f"passed at {ladder[learned].label} or above")
    if not reasons:
        reasons.append("the ladder's first cell")
    return ladder[index], reasons


def _index_of(ladder: Sequence[Cell], cell: Cell) -> int:
    if cell in ladder:
        return ladder.index(cell)
    key = cell.sort_key()
    below = [i for i, c in enumerate(ladder) if c.sort_key() <= key]
    return below[-1] if below else 0


def adopted(options: Options, adapters: Sequence[Adapter] | None) -> list[Adapter]:
    if adapters is not None:
        return list(adapters)
    return default_adapters(os.environ, livespec=options.livespec)


def plan(text: str, repo_dir: Path, store: Store, options: Options, registry: Mapping[str, caps.Capability],
         *, classifier: Callable[[str], Classification] | None = None,
         adapters: Sequence[Adapter] | None = None) -> Plan:
    catalog = roles.load()
    team, members, team_notes = _team(options, repo.key(repo_dir), catalog)
    covered = roles.kinds_of(members)
    if options.kind:
        classification = narrow(Classification(options.kind, "medium", "rule", "chosen by the caller"), covered)
    elif classifier is not None:
        classification = narrow(classifier(text), covered)
    else:
        classification = classify(text, use_model=options.use_model_classifier, cwd=home(), kinds=covered)
    kind = classification.kind

    neighbour_notes: list[str] = []
    statuses: list[Status] = []
    active: list[tuple[Adapter, Status]] = []
    context: list[str] = []
    raise_rungs, critical = 0, False
    # The symbols the request reaches, when an index could say: None when none could.
    reached: int | None = None
    for adapter in adopted(options, adapters):
        status = adapter.inspect(repo_dir)
        runnable_absent = status.state == ABSENT and getattr(adapter, "indexes", True) and \
            adapter.server() is not None
        if runnable_absent or status.stale:
            # In the background, never before the task: a large repository takes
            # minutes to index, and a run that is silent for minutes, with no task
            # yet to show, reads as one that never started. This task uses the
            # index as it is; the next one gets the fresh one.
            what = "index" if runnable_absent else "refresh"
            meanwhile = "this task runs without it" if runnable_absent else "this task uses the index as it is"
            failed = getattr(adapter, "last_failure", lambda _d: None)(repo_dir)
            if failed:
                # Said once: a failure every plan repeats until the checkout moves is noise.
                if options.dry_run or not getattr(adapter, "failure_said", lambda _d: False)(repo_dir):
                    neighbour_notes.append(f"{adapter.name}: its last index here failed ({clip(failed, 160)}); not "
                                           f"tried again until the checkout moves; {meanwhile}")
            elif adapter.server() is None:
                neighbour_notes.append(f"{adapter.name}: cannot {what} here; {meanwhile}")
            elif options.dry_run:
                neighbour_notes.append(f"{adapter.name}: would {what} in the background")
            elif adapter.refresh_in_background(repo_dir, home() / f"{adapter.name}-index.log"):
                neighbour_notes.append(f"{adapter.name}: {what}ing in the background; {meanwhile}")
            else:
                neighbour_notes.append(f"{adapter.name}: already {what}ing in the background; {meanwhile}")
        statuses.append(status)
        if not status.present:
            continue
        active.append((adapter, status))
        briefing = adapter.brief(text, repo_dir, status)
        context += briefing.lines
        raise_rungs = max(raise_rungs, briefing.raise_rungs)
        critical = critical or briefing.critical
        if briefing.reached is not None:
            reached = max(reached or 0, briefing.reached)
        neighbour_notes += briefing.reasons

    if critical and kind == "review-routine" and (not covered or "review-critical" in covered):
        kind = "review-critical"
        neighbour_notes.append("the code under review implements a critical spec: reviewed as critical")
    role, why = roles.pick(kind, members, repo_dir)
    neighbour_notes += team_notes
    risk = risk_mod.of_request(text, kind)
    size = points_mod.of_request(text, kind, risk, surfaces=options.surfaces, reached=reached)
    if role is not None:
        missing = [s for s in (*role.skills, *role.on_demand) if roles.find_skill(s) is None]
        if missing:
            neighbour_notes.append(f"role {role.name}: skill(s) {', '.join(missing)} not found; put each in "
                                   f"{home() / 'skills'}/<name>/SKILL.md or install it for Claude Code")
        absent = [c for c in role.capabilities if c not in registry and c not in {a.name for a, _ in active}]
        if absent:
            neighbour_notes.append(f"role {role.name}: capability {', '.join(absent)} is not in your registry "
                                   "or an adopted neighbour here; the workers run without it")
    ladder = ladder_for(kind)
    key = repo.key(repo_dir)
    # The standard of the task's band here: where tasks of its kind and size start, as measured.
    standard = standards.for_plan(store, key, kind, size.points, pinned=options.start is not None,
                                  explicit_turns=bool(options.max_turns)) if key else None
    if options.start is not None:
        start, reasons = options.start, ["pinned by the caller"]
    else:
        if standard is not None and standard.cell is not None:
            start, reasons = standard.cell, []
        else:
            start, reasons = choose_start(
                ladder, classification.complexity,
                store.landings(kind, repo=key) if key else [],
                store.landings(kind),
            )
        if raise_rungs:
            raised = min(_index_of(ladder, start) + raise_rungs, len(ladder) - 1)
            if ladder[raised] != start:
                start = ladder[raised]
                reasons.append(f"one rung up for what the code index found ({start.label})")
    if standard is not None:
        reasons += standard.reasons
    turns = None
    if not options.max_turns and standard is not None and standard.turns:
        turns = standard.turns
    elif not options.max_turns and key:
        raised_turns = turns_from_history(store.turn_history(kind, repo=key, limit=TURN_HISTORY), kind)
        if raised_turns:
            turns, why = raised_turns
            reasons.append(why)
    habits.load_events(store)
    learned = habits.recipes(store, kind, repo=repo.key(repo_dir))
    if learned:
        context.append("Steps that came before passing attempts on this kind of task here: "
                       + "; ".join(learned) + ". Use them unless the task says otherwise.")
    if kind not in READ_ONLY_KINDS and (flaky := known_flaky(store, repo_dir)):
        # Every worker met them on its own; what cauce saw is said once, to all of them.
        context.append("Tests known to flake in this repository (cauce saw each fail and then pass when the "
                       "same check ran again): " + "; ".join(flaky) + ". One of them failing alone says nothing "
                       "about your change: run it again before changing code for it, and name it in `summary`.")
    check = options.verify or options.run_verify
    neighbour_notes += _unmet(text, registry, served=bool(options.services), verify=check, where=repo_dir)
    if check and kind not in READ_ONLY_KINDS:
        neighbour_notes += _beside_ci(repo_dir, check, options.services, ci.quiet(store, repo.key(repo_dir)))
    if check and (weak := ci.weak(check, repo_dir)):
        neighbour_notes.append(weak)
    if check and (bypass := ci.bypassed(check, repo_dir)):
        neighbour_notes.append(bypass)
    if spans := _spans(repo_dir, options.add_dirs):
        neighbour_notes.append(spans)
    for lag in models.lagging(store):
        neighbour_notes.append(f"workers last ran {lag['workers']} for `{lag['alias']}` while your sessions use "
                               f"{lag['sessions']}: the installed Claude Code's alias is older. Pin it with "
                               f"`{lag['command']}`, or update Claude Code")
    mode = grants.mode_for(start.model)
    seen = _refused_here(store, repo.key(repo_dir), (*options.allow_tools, *grants.granted(repo.key(repo_dir))),
                         mode)
    if mode != "auto":
        neighbour_notes.append(f"workers on {start.model} run in {mode} mode")
    node = runtime.node_bin(repo_dir)
    if node is not None:
        neighbour_notes.append(f"workers run node {node.version} from {node.bin} ({node.source})")
    elif runtime.wanted(repo_dir) is None and (unmet := runtime.needed(repo_dir)[1]):
        neighbour_notes.append(unmet)
    if seen:
        neighbour_notes.append("workers here were refused before: " + ", ".join(f"{r} ×{n}" for r, n in seen)
                               + ". Pass the ones this task needs with --allow, or a worker stops on them")
    expected = store.expected_cost(kind, repo=repo.key(repo_dir)) or store.expected_cost(kind)
    if expected:
        neighbour_notes.append(f"finished {kind} tasks cost ${expected[0]:.2f} on average (last {expected[1]})")
    known: list[dict] = []
    key = repo.key(repo_dir)
    if key and config.enabled("notes", os.environ, options.notes):
        if not options.dry_run:
            notes.review(store, key, repo_dir)
        known = notes.recall(store, key, text, limit=6)
        if known:
            context += notes.brief_lines(known)
            neighbour_notes.append(f"notes: {len(known)} from this project's memory ("
                                   + ", ".join(f"#{n['id']} {n['topic']}" for n in known) + ")")
    selected = _with_role(caps.select(registry, kind, workdir=repo_dir, paths=touched_paths(text, options),
                                      reading=kind in READ_ONLY_KINDS), role, registry, repo_dir)
    names = tuple(dict.fromkeys([*selected.names, *(a.name for a, _ in active)]))
    race, race_why = race_cell(store, key, kind, ladder, start, options)
    if race_why:
        reasons.append(race_why)
    return Plan(kind, classification, ladder, start, reasons + neighbour_notes, names,
                tuple(context), tuple(statuses), tuple(active), tuple(known), role, team, why,
                risk, turns, size, standard.data() if standard is not None else None, race)


#: A start whose first tries passed less often than this here, over at least `RACE_SAMPLES`
#: of them, races the next rung on its first try (`race_cell`).
RACE_BELOW = 0.5
RACE_SAMPLES = 4


def race_cell(store: Store, key: str | None, kind: str, ladder: Sequence[Cell], start: Cell,
              options: Options) -> tuple[Cell | None, str]:
    """The rung a task's first try races beside its start, and why; (None, "") when it does
    not race. A climb after a first try that failed is a whole attempt more of waiting: where
    the start's first tries pass less than `RACE_BELOW` of the time in this repository, the
    next rung that needs no approval runs at the same time, each in its own worktree, and
    the first that passes its check is kept (the other is stopped). Money is no brake, so the
    second worker is the price of the time. Never a pinned start, a reading kind, a step that
    decides a ledger, joins parts or plans them, work in the person's checkout, or with
    `race` off. Racing judges itself (`racing.judge`): where its record says it rarely
    paid here, the start does not race, the climb goes deep instead, and the reason says
    so beside no rung."""
    if (not key or options.start is not None or kind in READ_ONLY_KINDS or not options.isolate
            or options.launch_dir or options.ledger or options.integrate or options.parts
            or not config.enabled("race", os.environ)):
        return None, ""
    after = list(ladder)[_index_of(ladder, start) + 1:]
    rung = next((c for c in after if not c.needs_approval), None)
    if rung is None:
        return None, ""
    mine = [t for t in standards.ended(store, repo=key) if t.kind == kind]
    window = mine[-standards.WINDOW:]
    passed, tried = standards.rates(window).get(start.label, [0, 0])
    if tried < RACE_SAMPLES or passed / tried >= RACE_BELOW:
        return None, ""
    outgrew, failed = _outgrew(window, start.label)
    if failed and outgrew * 2 >= failed:
        # Out of turns is room, never the cell: a rung beside it outgrows the same turns.
        return None, (f"its first try does not race {start.label} against {rung.label}, though {passed} of {tried} "
                      f"first tries at {start.label} passed here: {outgrew} of the {failed} that failed ran out of "
                      "turns, which another cell does not fix; work this size passes in smaller tasks or with more "
                      "turns")
    record = racing.judge(window, mine)
    if not record.pays:
        return None, (f"its first try does not race {start.label} against {rung.label}, though {passed} of {tried} "
                      f"first tries at {start.label} passed here: {record.why}")
    return rung, (f"its first try races {start.label} against {rung.label}: {passed} of {tried} first tries at "
                  f"{start.label} passed here; the first to pass its check is kept, the other stopped"
                  + (f" ({record.why})" if record.races else ""))


def _outgrew(tasks: Sequence[standards.Ended], label: str) -> tuple[int, int]:
    """Of the tasks whose first try ran on `label` and failed, how many ran out of turns on it,
    and how many failed: a race gives the try another cell, never more room."""
    outgrew = failed = 0
    for task in tasks:
        first = standards.samples(task.attempts) if not task.pinned else []
        if not first or first[0][0] != label or first[0][1]:
            continue
        failed += 1
        outgrew += any(a.get("failure") == Failure.TURNS_EXHAUSTED.value and a.get("cell") == label
                       for a in task.attempts)
    return outgrew, failed


@dataclass
class Raced:
    """What a race's first try came to: the try that goes on (the one that passed, else the
    higher rung's, unless only the environment stopped it) and the one recorded beside it."""

    result: launch.WorkerResult
    cell: Cell
    spec: launch.LaunchSpec
    other: launch.WorkerResult
    other_cell: Cell
    #: The kept try's check already ran, in the worktree its work came from.
    checked: bool


#: The move a race's stopped try is kept with: it says nothing of its cell (`standards.samples`).
RACED_OUT = racing.RACED_OUT


def _record_raced(store: Store, task_id: int, raced: Raced, turns: int, capabilities: Sequence[str],
                  report: Report, *, first: bool) -> int:
    """Keep the try a race did not go on with, as an attempt of its own: its cost, its cell
    and what it said. When it is the start's (`first`), the rung's that goes on follows it as
    a new start (`race`); a try stopped because the other passed first teaches no cell."""
    o = raced.other
    stopped = o.summary.startswith("stopped:")
    seq = store.add_attempt(
        task_id, cell=raced.other_cell.label, max_turns=turns, passed=0,
        failure=(o.failure or Failure.INCONCLUSIVE).value, summary=o.summary, evidence=kept_evidence(o.evidence),
        cost_usd=o.cost_usd, input_tokens=o.input_tokens, output_tokens=o.output_tokens, turns=o.turns,
        duration_s=o.duration_s, changed_paths=o.changed_paths, served_model=o.served_model or None,
        capabilities=capabilities)
    store.add_event(task_id, "attempt_finished", seq=seq, cell=raced.other_cell.label, passed=False,
                    failure=(o.failure or Failure.INCONCLUSIVE).value, cost_usd=o.cost_usd, turns=o.turns,
                    summary=o.summary[:500], raced=True, stopped=stopped, wall_s=round(o.duration_s, 2),
                    input_tokens=o.input_tokens, output_tokens=o.output_tokens,
                    cache_read_tokens=o.cache_read_tokens, cache_write_tokens=o.cache_write_tokens,
                    worker_cost_usd=round(o.cost_usd, 4), served_model=o.served_model)
    if stopped:
        store.set_move(task_id, seq, RACED_OUT, "the other try passed its check first")
    elif first:
        store.set_move(task_id, seq, "race", f"raced {raced.cell.label} beside it")
    report.cells.append(raced.other_cell.label)
    report.served.append(o.served_model)
    report.cost_usd += o.cost_usd
    return seq


def _raced_attempt(raced: Raced, turns: int) -> Attempt:
    o = raced.other
    return Attempt(raced.other_cell, turns, False, o.failure or Failure.INCONCLUSIVE, o.summary, o.denied,
                   allow=o.allow, changed=o.changed_paths, evidence=o.evidence[-1500:], transient=o.transient,
                   findings=o.findings)


def _launch(launcher: Launcher, spec: launch.LaunchSpec, runner: launch.Stoppable) -> launch.WorkerResult:
    if launcher is launch.run:
        return launch.run(spec, runner=runner)
    return launcher(spec)


def _race(launcher: Launcher, spec: launch.LaunchSpec, start: Cell, rung: Cell, workspace: isolate.Workspace,
          verify: str | None, baseline: Baseline | None = None) -> Raced | None:
    """Run the start and the rung side by side, each in its own worktree, each checked in it:
    the first that passes is kept and the other stopped. The task's worktree ends holding the
    kept try's work. None when the second worktree cannot be made: the try runs alone."""
    try:
        lane = isolate.race_lane(workspace, home())
    except (isolate.IsolationError, OSError):
        return None
    there = str(lane.path)
    second = dataclasses.replace(
        spec, cell=rung, model_id=models.pinned(rung.model), permission_mode=grants.mode_for(rung.model),
        launch_dir=lane.path, prompt=spec.prompt.replace(str(workspace.path), there),
        env={**spec.env, "CAUCE_WORKER_ATTEMPT": spec.env.get("CAUCE_WORKER_ATTEMPT", "") + "-race"},
        resume_session="", fork_session=False)
    runners = (launch.Stoppable(), launch.Stoppable())

    def side(i: int, run_spec: launch.LaunchSpec, where: Path) -> launch.WorkerResult:
        found = _launch(launcher, run_spec, runners[i])
        if found.passed and verify and not runners[i].stopped:
            found = _verified(found, verify, where, baseline)
        return found

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(side, 0, spec, workspace.path): 0, pool.submit(side, 1, second, lane.path): 1}
            done: dict[int, launch.WorkerResult] = {}
            pending = set(futures)
            winner = None
            while pending and winner is None:
                finished, pending = wait_for(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    done[futures[future]] = future.result()
                    if done[futures[future]].passed and winner is None:
                        winner = futures[future]
                if winner is not None:
                    for i in (0, 1):
                        if i != winner:
                            runners[i].stop()
            for future in pending:
                done[futures[future]] = future.result()
        if winner is None:
            # Neither passed: the climb goes on from the higher rung, unless only the environment stopped it.
            winner = 0 if done[1].failure is Failure.ENVIRONMENT and done[0].failure is not Failure.ENVIRONMENT else 1
        loser = 1 - winner
        lost = done[loser]
        if done[winner].passed:
            lost = dataclasses.replace(lost, passed=False, summary=clip(
                f"stopped: {(start, rung)[winner].label} passed its check first. {lost.summary}", 2000))
        kept = done[winner]
        if winner == 1:
            isolate.adopt(lane, workspace)
            # Its session lived in the race's worktree, which goes: what follows starts fresh.
            kept = dataclasses.replace(kept, session_id="")
        return Raced(kept, (start, rung)[winner], (spec, second)[winner], lost, (start, rung)[loser],
                     checked=bool(verify) and kept.passed)
    finally:
        isolate.drop(lane)


def touched_paths(text: str, options: Options, changed: Sequence[str] = ()) -> list[str]:
    """What a task's work touches, for the capabilities about those files: the files and
    folders its request names, its surfaces, the diff a workflow step reads, and what its
    work changed so far."""
    return list(dict.fromkeys([*points_mod.named(text, options.surfaces), *options.touches, *changed]))


def turns_from_history(runs: Sequence[Sequence[tuple[int, bool, str | None]]], kind: str) -> tuple[int, str] | None:
    """The turns a task of `kind` starts with when the recent ones here outgrew the kind's
    own and passed with more (each run its attempts' `(max_turns, passed, failure)`): the
    median of what those passes needed, capped. Only ever above the kind's own; None
    when fewer than `MIN_TURN_SAMPLES` needed more."""
    own = turns_for(kind)
    needed = []
    for attempts in runs:
        passes = [turns for turns, passed, _ in attempts if passed]
        if passes and passes[-1] > own and any(
                failure == Failure.TURNS_EXHAUSTED.value and turns <= own for turns, _, failure in attempts):
            needed.append(passes[-1])
    if len(needed) < MIN_TURN_SAMPLES:
        return None
    turns = min(int(statistics.median_low(needed)), MAX_TURNS_CEILING)
    return turns, (f"starts with {turns} turns: {len(needed)} of the last {len(runs)} {kind} tasks here "
                   f"needed more than {own}")


#: How the plan's reasons a person should hear before the work starts begin: what
#: the task needs and workers lack, and what stopped workers here before.
WARNINGS = ("this task needs", "this task mentions", "this task spans", "workers here were refused before", "task #",
            "CI also runs", "this task's check")


def warnings(reasons: Sequence[str]) -> list[str]:
    return [r for r in reasons if r.startswith(WARNINGS)]


def foresee(text: str, repo_dir: Path, store: Store, options: Options) -> list[str]:
    """What a task's plan will warn of when it starts, said before it does: a dry
    plan, classified by rules alone and with no neighbour asked, since a neighbour's
    notes warn of nothing."""
    dry = dataclasses.replace(options, dry_run=True, use_model_classifier=False)
    registry = caps.load_all(home() / "capabilities.json")
    return warnings(plan(text, repo_dir.resolve(), store, dry, registry, adapters=()).reasons)


def _team(options: Options, key: str | None, catalog: roles.Catalog) -> tuple[str, list[roles.Role], list[str]]:
    """The roles a task here may run as: the one a person named, or the project's team."""
    lines: list[str] = []
    if options.role:
        try:
            if options.role in roles.INTERNAL and options.role not in catalog.roles:
                return "cauce's own", [roles.internal(options.role, catalog)], []
            return "named for this run", [catalog.get(options.role)], []
        except roles.RoleError as exc:
            # A queued or resumed task whose role is gone since: it runs, as its team.
            lines.append(f"{exc}: the project's team serves")
    team, why = teams.of(key, catalog)
    members, missing = team.members(catalog)
    lines += [] if team.name == teams.BUNDLED else [why]
    if options.step_role:
        named = [r for r in members if r.name == options.step_role]
        if named:
            return team.name, named, [*lines, f"the workflow step runs as {options.step_role}, of this team"]
        lines.append(f"the workflow step names {options.step_role}, which is not in the {team.name} team: "
                     "the team picks the role")
    if missing:
        lines.append(f"the {team.name} team names role(s) that are not there: {', '.join(missing)}")
    if catalog.refused:
        lines.append("roles refused: " + "; ".join(f"{n} ({why})" for n, why in sorted(catalog.refused.items())))
    if not members:
        # A team whose roles are all gone limits nothing: the bundled one serves.
        bundled = teams.bundled(catalog)
        lines.append(f"the {team.name} team has no role left: the {bundled.name} team serves")
        return bundled.name, bundled.members(catalog)[0], lines
    return team.name, members, lines


def _with_role(selection: caps.Selection, role: roles.Role | None, registry: Mapping[str, caps.Capability],
               workdir: Path) -> caps.Selection:
    """The capabilities the role names, from the person's registry, beside the ones
    the kind gets. A role names capabilities; it never brings one."""
    if role is None:
        return selection
    wanted = {name: registry[name] for name in role.capabilities if name in registry
              and name not in selection.servers}
    if not wanted:
        return selection
    extra = caps.select(wanted, "", workdir=workdir, every=True)
    return caps.Selection({**selection.servers, **extra.servers}, (*selection.hints, *extra.hints),
                          (*selection.names, *extra.names), (*selection.read_dirs, *extra.read_dirs))


def run(
    text: str,
    repo_dir: Path,
    store: Store,
    options: Options | None = None,
    *,
    registry: Mapping[str, caps.Capability] | None = None,
    launcher: Launcher | None = None,
    classifier: Callable[[str], Classification] | None = None,
    adapters: Sequence[Adapter] | None = None,
    task_id: int | None = None,
    session_id: str | None = None,
    history: Sequence[Attempt] = (),
    notes_ask: notes.Ask | None = None,
    progress: Callable[[str], None] | None = None,
    known_findings: Sequence[Mapping[str, str]] = (),
) -> Report:
    """Run a task. `history` is a resumed task's earlier attempts: shown to its
    workers, never counted by the escalation, which starts over. `known_findings`
    are what the independent check found before a resume: its first check says for
    each whether it is fixed."""
    options = options or Options()
    # Wall time of the run and of its phases, kept on its events: where a task's
    # time goes (waiting, setting up, working, checking) is evidence for the router.
    clock, began = time.monotonic(), time.time()
    registry = registry if registry is not None else caps.load_all(home() / "capabilities.json")
    launcher = launcher or launch.run
    repo_dir = repo_dir.resolve()
    the_plan = plan(text, repo_dir, store, options, registry, classifier=classifier, adapters=adapters)
    plan_s = time.monotonic() - clock
    if options.dry_run:
        return Report(None, "dry run", the_plan)
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(repo_dir)  # work run here: the project is cauce's

    key = repo.key(repo_dir)
    c = the_plan.classification
    fields = dict(
        repo=key, cwd=str(repo_dir), kind=the_plan.kind, complexity=c.complexity, class_source=c.source,
        class_reason=c.reason, start_cell=the_plan.start.label, pinned=int(options.start is not None),
        points=the_plan.points.points, points_why=", ".join(the_plan.points.reasons),
    )
    queued_s = None
    # The time before this run that no phase of its own measures: from the step it waited on
    # passing to its being queued, from queued to the dispatcher starting it, and from that to here.
    gaps = _gaps(store, task_id, began) if task_id is not None else {}
    if task_id is not None:
        # A queued or resumed task: it keeps its id, its messages, its cost so
        # far and its place in the lane's history.
        before = store.get_task(task_id)
        if before["status"] == "queued" and not store.last_event(task_id, "finished"):
            queued_s = _seconds_since(before["created_at"])
        store.update_task(task_id, status="running", source="cauce",
                          cost_usd=float(before["cost_usd"] or 0) + c.cost_usd, **fields)
        task = store.get_task(task_id)
    else:
        # Run from a session's own Bash (`CAUCE_SESSION_ID` set), the text is the main
        # session's, not something a person typed.
        task = store.create_task(text, status="running", source="cauce", cost_usd=c.cost_usd,
                                 author="orchestrator" if session_id else "person",
                                 session_id=session_id, options=json.dumps(options.stored()), **fields)
    report = Report(task["id"], "running", the_plan, cost_usd=c.cost_usd)
    store.update_task(task["id"], pid=os.getpid())
    say = progress or (lambda line: None)
    # Said as it happens: a run in a background shell is otherwise silent until
    # its report, and silence reads as a run that never started.
    say(f"cauce: task #{task['id']} started: {the_plan.kind}, at {the_plan.start.label}")
    if task_id is not None and not store.last_event(task["id"], "planned") and (
            released := after.released_line(store, task["id"])):
        # Queued after other work that passed: where it starts, said once, as it starts.
        the_plan.reasons.append(released)
    if task_id is None:
        for twin in _twins(store, task, repo_dir):
            the_plan.reasons.append(f"task #{twin} asked the same and keeps its work on {isolate.branch_for(twin)}: "
                                    f"`cauce resume {twin}` continues it instead of starting over")
    if options.max_turns and options.max_turns > MAX_TURNS_CEILING:
        the_plan.reasons.append(f"{options.max_turns} turns were asked for; an attempt gets at most "
                                f"{MAX_TURNS_CEILING}: work bigger than that is split")
    if not options.max_turns and the_plan.turns is None and turns_for(the_plan.kind) != DEFAULT_TURNS:
        the_plan.reasons.append(f"{the_plan.kind} work starts with {turns_for(the_plan.kind)} turns: "
                                f"at {DEFAULT_TURNS} its attempts ran out")
    warned = warnings(the_plan.reasons)
    for reason in warned:
        say(f"cauce: #{task['id']} warning: {reason}")
    store.add_event(task["id"], "planned", kind=the_plan.kind, start=the_plan.start.label, warnings=warned,
                    role=the_plan.role.name if the_plan.role else None, team=the_plan.team,
                    risk=the_plan.risk.data(), points=the_plan.points.points,
                    points_why=list(the_plan.points.reasons), standard=the_plan.standard,
                    ladder=[x.label for x in the_plan.ladder], reasons=the_plan.reasons,
                    neighbours=[st.line() for st in the_plan.neighbours], plan_s=round(plan_s, 2),
                    queued_s=queued_s)

    workspace = None
    filled: list[str] = []
    resumed = False
    # Why the task's worktree could not be made, in one line. It stops the task before its first
    # attempt, resumable, instead of leaving it `running` under a process that is about to exit.
    no_worktree: str | None = None
    writes = the_plan.kind not in READ_ONLY_KINDS
    declared_read_only = declares_read_only(the_plan.kind, task.get("body") or task.get("title") or "")
    if not options.verify and options.run_verify and writes and not declared_read_only:
        # A workflow step with no kind of its own was classified into one that writes:
        # the run's check is its check.
        options = dataclasses.replace(options, verify=options.run_verify)
    context = list(the_plan.context)
    # A reading step of a workflow reads the work of the steps before it: it gets
    # their commit too, in a worktree its tools cannot write to.
    if options.isolate and (writes or options.base) and repo.toplevel(repo_dir) is not None:
        try:
            resumed = isolate.kept(repo_dir, task["id"])
            workspace = isolate.prepare(repo_dir, task["id"], home(), base=options.base)
            if resumed:
                context.append("This task was stopped before and is resumed: what its earlier attempts wrote is "
                               "already here. Check it, and finish it rather than starting over.")
            if workspace.links:
                context.append("Linked from the person's checkout and shared with it: "
                               + ", ".join(workspace.links) + ". Use them; do not delete or reinstall them.")
            if workspace.stale:
                # Linking them would hand the worker another commit's dependencies.
                context.append("Not linked from the person's checkout: " + ", ".join(workspace.stale)
                               + ". The lockfile here differs from the one they were installed from there, so "
                               "install the dependencies in this directory first (`npm ci`, `uv sync`, as the "
                               "project does).")
                say(f"dependencies not linked, their lockfile differs from your checkout's: "
                    f"{', '.join(workspace.stale)}; the worker installs them (a refused install is allowed "
                    f"with --allow, e.g. 'Bash(npm ci)')")
            if not resumed:
                filled = _fill_worktree(store, task["id"], workspace, text, options.carry, say)
                context += filled
        except (isolate.IsolationError, OSError) as exc:  # git failed, or git or the disk is not there
            no_worktree = " ".join(str(exc).split())
    if workspace is not None and task.get("workflow_run"):
        # How far the base branch moved past the work this step starts from: said, never merged.
        drift = isolate.behind_text(workflows.drift(store, task, workspace.base, "start") or {})
        if drift:
            say(f"cauce: #{task['id']} starts {drift}")
    workdir = workspace.path if workspace else repo_dir
    context += how_to_run(workdir, (*options.allow_tools, *grants.granted(key)))
    # The thread of the session that sent it, as the scribe kept it: the worker
    # does not start from the task's text alone.
    from_session, got = _session_context(task, text)
    context += from_session
    launch_dir = options.launch_dir.resolve() if options.launch_dir else workdir

    # Strict, as the prompt hook: a task's text shares a word or two with
    # nearly every task in its repository, and a loose match puts unrelated
    # failures in front of the worker as if they were this problem's. Matched by
    # the work's own text (a step's goal, never its template) and kept to its
    # cell: the failures of its area, of its role, and the shared ones.
    reach = cells.of_request(text, the_plan.kind, touched_paths(text, options))
    dead = cells.dead_ends(store, cells.memory_text(store, task), repo=key, areas=reach,
                           role=the_plan.role.name if the_plan.role is not None else None)
    if dead:
        # What the workers are shown, kept as shown: the memory grows after the run.
        store.add_event(task["id"], "dead_ends", shown=dead)
    # How the cell's earlier workers got oriented: given or withheld by the primer's
    # trial, and kept either way so the two arms can be compared.
    role_name = the_plan.role.name if the_plan.role is not None else None
    primed: dict[str, Any] = {}
    if config.enabled("primer", os.environ):
        lines = cells.primer(store, key, role=role_name, areas=reach)
        if lines:
            arm, why = cells.primer_arm(store, key, task["id"])
            primed = {"primer_arm": arm, "primer_lines": len(lines), "primer_why": why}
            if arm == "primer":
                context.append(cells.primer_text(lines))
    # Which role and which version of its text this worker wears, and the role's lessons here:
    # given or withheld by their trial, and kept either way so the arms can be compared.
    # The notes the brief carries are kept too, so a worker-learned one is proven or dropped by
    # the tasks that read it (`notes.weigh`), never by a guess.
    worn: dict[str, Any] = {}
    if the_plan.role is not None:
        worn = {"role": role_name, "role_version": roles.version(the_plan.role)}
        if revisions.enabled():
            lessons, given = revisions.for_brief(store, key, role_name, task["id"])
            worn.update(given)
            if lessons:
                context.append(lessons)
    store.add_event(task["id"], "briefed", session=got, dead_ends=len(dead), areas=list(reach),
                    context_chars=sum(len(line) for line in context), **primed, **worn,
                    **({notes.BRIEFED: [int(n["id"]) for n in the_plan.notes]} if the_plan.notes else {}))
    # The runtime the project declares goes first on the worker's PATH, and on its
    # check's: a worker given the wrong Node hunts for another and is refused each one.
    run_env = runtime.env_for(workdir)
    briefing = roles.brief(the_plan.role) if the_plan.role is not None else None
    # The independent check: what it found first, and how many corrections it has had.
    verifying = writes and not declared_read_only and config.enabled("verifier", os.environ) \
        and not options.reviewed_next
    if writes and options.reviewed_next and config.enabled("verifier", os.environ):
        the_plan.reasons.append("no independent check of its own: the blind review step that follows reads "
                                "this change as the verifier would")
    ledger: dict[str, Mapping[str, str]] = {findings_mod.key(f): f for f in known_findings}
    corrections = 0
    # A workflow step that decides ledger rows: a pass answers each, and its answers decide the run.
    deciding = options.ledger if options.ledger and options.ledger.get("role") in ledger_mod.WORDS else None
    settled: ledger_mod.Outcome | None = None
    base_commit = workspace.base if workspace else _head(repo_dir)
    # A red check is compared with the same check at the commit the work started from:
    # what failed there too was not this work's doing (`_against_base`).
    baseline = Baseline(store, task["id"], workspace.repo_dir if workspace else repo_dir, base_commit, home(),
                        must_fix=the_plan.kind in _REPRODUCES) if base_commit and options.verify else None
    # Turns are the one room dial, and they have one ceiling: a number asked for from
    # outside (a resume, a model's tool call) never raises it.
    cell, turns = the_plan.start, min(options.max_turns or the_plan.turns or turns_for(the_plan.kind),
                                      MAX_TURNS_CEILING)
    attempts: list[Attempt] = []
    changed: set[str] = set()
    decision: Decision | None = None
    # What the brief says of work set aside when a new model started from the base.
    aside: list[str] = []
    # The last attempt's session, and what ran it: more turns on the same cell continue
    # it, more effort on the same model continues a fork of it.
    session: WorkerSession | None = None
    # The session the next attempt continues, or why one due starts fresh: decided with the move.
    resume, fresh = "", ""
    # The commit's subject: the last line a worker proposed for the change.
    subject = ""
    # In the checkout itself, one run at a time: the work and its check share the
    # person's files. A run in a worktree has its own.
    in_place = no_worktree is None and workspace is None and (
        (writes and not declared_read_only) or bool(options.verify))
    held = contextlib.ExitStack()
    # What an integrate step joined before its first attempt (`_integrate`).
    joined: dict[str, Any] | None = None
    timing = {"hold_s": 0.0, "services_s": 0.0, "worker_s": 0.0, "check_s": 0.0, "verifier_s": 0.0, "wait_s": 0.0}
    # Its first try races the next rung (`race_cell`): only a fresh run in its own worktree
    # that brought nothing in from the person's checkout, which the race's lane would lack.
    racing = the_plan.race is not None and workspace is not None and not history and not filled and not resumed
    try:
        mark = time.monotonic()
        if in_place:
            for place in isolate.held_by(repo_dir, repo.toplevel(repo_dir)):
                held.enter_context(isolate.in_place(
                    home(), place, task["id"], waiting=lambda who: _waits(store, task["id"], who, say),
                    check=lambda: _check_cancel(store, task["id"])))
        timing["hold_s"] = time.monotonic() - mark
        ready = no_worktree is None
        if not ready:
            report.stop = stops.Stop("environment", f"its worktree could not be made: {no_worktree}"[:500],
                                     extra={"starved": "disk"} if launch.NO_SPACE.search(no_worktree or "") else {})
            report.status, report.summary = "blocked", report.stop.reason
        elif options.services:
            mark = time.monotonic()
            try:
                up = held.enter_context(services_mod.running(
                    [services_mod.parse(s) for s in options.services], workdir,
                    home() / "work" / f"services-{task['id']}.log", run_env))
            except services_mod.ServiceError as exc:
                # The servers the task needs never came up: nothing a worker does helps.
                report.stop = stops.Stop("environment", str(exc))
                report.status, report.summary, ready = "blocked", str(exc), False
            else:
                timing["services_s"] = time.monotonic() - mark
                say(f"cauce: #{task['id']} servers up: " + "; ".join(up))
                context.append("Servers up for this task, started and stopped by cauce (do not start or stop them "
                               "yourself): " + "; ".join(up))
        if ready and options.integrate:
            # The parts' branches are joined before any worker: one is needed only for a conflict,
            # or for a check that fails on the joined work.
            joined, ready = _integrate(store, task["id"], workspace, options, report, context, say, baseline)
        while ready:
            # A try after the network dropped, or the load timed its tests out, is no
            # attempt at the work: it does not count toward the ceiling, and its own
            # streak is bounded in `decide`.
            if sum(not waited(a) for a in attempts) >= options.max_attempts:
                if attempts and attempts[-1].blockers:
                    # The work passed its own check; only the independent check's blockers stand.
                    report.stop = stops.Stop(
                        "review", review_reason(attempts[-1], f"the limit of {options.max_attempts} attempts"),
                        attempt=len(attempts), account=findings_mod.text(attempts[-1].blockers)[:2000])
                    report.status, report.summary = "blocked", report.stop.reason
                else:
                    report.stop = stops.Stop("attempts", f"{options.max_attempts} attempts without a pass",
                                             attempt=len(attempts))
                    report.status, report.summary = "failed", report.stop.reason
                break
            _check_cancel(store, task["id"])
            selection = _equip(_with_role(caps.select(
                registry, the_plan.kind, failed_before=bool(attempts), workdir=workdir,
                paths=touched_paths(text, options, [*changed, *(isolate.changed(workspace) if workspace else ())]),
                reading=not writes or declared_read_only), the_plan.role, registry, workdir), the_plan, repo_dir,
                workdir)
            # Numbered as the store numbers it: a resumed task's attempts go on from its last.
            nth = len(store.attempts(task["id"])) + 1
            fork = bool(resume) and decision is not None and decision.move is Move.MORE_EFFORT
            spec = launch.LaunchSpec(
                model_id=models.pinned(cell.model),
                permission_mode=grants.mode_for(cell.model),
                prompt=brief(text, the_plan.kind, workdir, [*history, *attempts], dead, options.verify,
                             writes and not declared_read_only, [*context, *aside], options.surfaces),
                cell=cell,
                launch_dir=launch_dir,
                workdir=workdir if workdir != launch_dir else None,
                max_turns=turns,
                timeout_s=launch.timeout_for(turns),
                disallowed_tools=tuple(dict.fromkeys([*(() if writes else launch.WRITE_TOOLS),
                                                      *(the_plan.role.disallowed_tools if the_plan.role else ())])),
                # The person's rules, for this run and for this repository, the
                # tools of every MCP server cauce itself hands this worker (a
                # capability it was given and then refused is no capability), and
                # reading the skill folders of its role.
                allowed_tools=tuple(dict.fromkeys([*options.allow_tools, *grants.granted(key),
                                                   *(f"mcp__{name}" for name in selection.servers),
                                                   *(roles.read_rules(briefing) if briefing else ()),
                                                   *(f"Read(/{Path(d).resolve()}/**)" for d in selection.read_dirs)])),
                extra_dirs=(*(tuple(workspace.repo_dir / rel for rel in workspace.links
                                    if (workspace.repo_dir / rel).is_dir()) if workspace else ()),
                            *(Path(d).expanduser().resolve() for d in options.add_dirs)),
                mcp_servers=selection.servers,
                append_system_prompt="\n\n".join(p for p in (briefing.system_prompt if briefing else "",
                                                               selection.system_prompt()) if p),
                env={**run_env, "CAUCE_WORKER_TASK": str(task["id"]), "CAUCE_WORKER_ATTEMPT": str(nth)},
                criteria=criteria_mod.ids(text),
                judging=not writes or declared_read_only or deciding is not None,
                verdicts=tuple(deciding.get("ids") or ()) if deciding else (),
                verdict_words=ledger_mod.WORDS.get(deciding["role"], ()) if deciding else (),
                parts=options.parts,
                # Kept only in a worktree, where its directory is the task's: a session in the
                # person's checkout would be one of theirs.
                persist_session=workspace is not None,
                resume_session=resume,
                fork_session=fork,
                resume_prompt=(launch.DEEPER_PROMPT if fork else launch.RESHAPE_PROMPT
                               if decision is not None and decision.reshape else launch.CONTINUE_PROMPT),
                resume_note=continuation_note([*history, *attempts][-1]) if resume else "",
            )
            store.update_task(task["id"], current_cell=cell.label)
            say(f"cauce: #{task['id']} attempt {nth} at {cell.label}")
            store.add_event(task["id"], "attempt_started", seq=nth, cell=cell.label,
                            **({"base": workspace.base, "links": list(workspace.links)} if workspace else {}),
                            model=spec.model_id or cell.model,
                            max_turns=turns, capabilities=list(selection.names),
                            since_start_s=round(time.monotonic() - clock, 2), brief_chars=len(spec.prompt),
                            **({"role": the_plan.role.name, "team": the_plan.team,
                                "skills": list(briefing.skills)} if briefing else {}),
                            **({"resumed": resume} if resume else {}), **({"fresh": fresh} if fresh else {}))
            if resume:
                say(f"cauce: #{task['id']} attempt {nth} continues attempt {nth - 1}'s session ({resume})"
                    + (f" at {cell.label}" if fork else ""))
            elif fresh:
                say(f"cauce: #{task['id']} attempt {nth} starts fresh: {fresh}")
            mark = time.monotonic()
            raced, raced_first = None, False
            if racing and the_plan.race is not None and workspace is not None:
                racing = False
                store.add_event(task["id"], "attempt_started", seq=nth + 1, cell=the_plan.race.label, race_of=nth,
                                model=models.pinned(the_plan.race.model) or the_plan.race.model, max_turns=turns,
                                since_start_s=round(time.monotonic() - clock, 2))
                say(f"cauce: #{task['id']} attempt {nth} races {the_plan.race.label} beside {cell.label}")
                raced = _race(launcher, spec, cell, the_plan.race, workspace, options.verify, baseline)
            if raced is not None:
                # What follows (the independent check, the next attempt) works in the task's worktree,
                # which holds the kept try's work: the spec keeps its place, with the kept try's cell.
                result = raced.result
                spec = dataclasses.replace(spec, cell=raced.cell, model_id=raced.spec.model_id,
                                           permission_mode=raced.spec.permission_mode)
                if raced.cell != cell:
                    # The higher rung's try goes on: the start's is recorded before it.
                    _record_raced(store, task["id"], raced, turns, selection.names, report, first=True)
                    attempts.append(_raced_attempt(raced, turns))
                    nth, raced_first = nth + 1, True
                cell = raced.cell
            else:
                result = launcher(spec)
            if result.fresh:
                say(f"cauce: #{task['id']} attempt {nth} started fresh: {result.fresh}")
            session = WorkerSession(result.session_id if spec.persist_session else "", spec.model_id or cell.model,
                                    cell, nth)
            worker_s = time.monotonic() - mark
            # Out of turns (or time) before its own check: whatever the check finds next,
            # the work had no room to finish, and room is what the next attempt gets.
            outgrew = result.failure is Failure.TURNS_EXHAUSTED
            mark = time.monotonic()
            # What cauce's own check writes in the worktree is the check's output, not the
            # worker's: it is committed with the work, and said, since a check that rewrites
            # tracked files may not give the same files on the person's checkout.
            before = isolate.fingerprint(workspace.path) if workspace is not None and options.verify else None
            if baseline is not None and not (raced is not None and raced.checked):
                baseline.found = None
            if result.passed and options.verify and not (raced is not None and raced.checked):
                result = _verified(result, options.verify, workdir, baseline)
            elif _settles(result, options.verify) and (
                    isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
                result = _settled(result, options.verify, workdir, baseline)
            stands = ""
            if result.failure in REPLAN and not result.denied and (
                    isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
                # A worker that did the work and names a point it cannot settle: does the work stand?
                stands = _stands(result, options.verify, workdir, baseline)
                if stands == "cauce" and _only_the_base(result, options.verify, baseline):
                    # Its open point is the check's failures, and they were all there before
                    # the work: nothing is left to decide, and the work passes as it stands.
                    result = dataclasses.replace(
                        result, passed=True, failure=None, verdict="pass",
                        summary=f"{result.summary} (cauce: the check's failures it names all fail at the base "
                                "too, so they were there before this work and no decision is left)")
                    stands = ""
            # What the report and the verdict say of a check that failed only as it does at the base.
            report.preexisting = baseline.found if baseline is not None and (result.passed or stands) else None
            if before is not None and workspace is not None and \
                    (wrote := isolate.rewritten(before, isolate.fingerprint(workspace.path))):
                report.check_output = sorted({*report.check_output, *wrote})
                store.add_event(task["id"], "check_rewrote", command=options.verify, n=len(wrote),
                                files=list(wrote[:200]))
            check_s = time.monotonic() - mark
            strayed = surfaces_mod.outside(options.surfaces, result.changed_paths)
            if strayed:
                if workspace is not None:
                    isolate.undo(workspace, strayed)
                kept = "undone" if workspace is not None else "left in your checkout, unverified"
                result = dataclasses.replace(
                    result, passed=False, failure=Failure.APPROACH,
                    changed_paths=tuple(p for p in result.changed_paths if p not in strayed)
                    if workspace is not None else result.changed_paths,
                    summary=f"changed outside its surfaces ({', '.join(strayed[:5])}), {kept}: {result.summary}",
                    evidence=f"Surfaces: {', '.join(options.surfaces)}. Changed outside them: {', '.join(strayed)}")
                store.add_event(task["id"], "outside_surfaces", seq=nth, paths=strayed[:50],
                                surfaces=list(options.surfaces), undone=workspace is not None)
            review_stop = None
            open_blockers: tuple[dict[str, str], ...] = ()
            mark = time.monotonic()
            worker_cost = result.cost_usd
            if result.passed and verifying:
                so_far = list(isolate.changed(workspace)) if workspace else sorted({*changed, *result.changed_paths})
                risk_mod.of_change(the_plan.risk, so_far if (workspace or base_commit) else None)
                if so_far and risk_mod.needs_verifier(the_plan.risk, cheap=_cheap(cell)):
                    result, review_stop, ledger, corrections, open_blockers = _independent_check(
                        result, text=text, kind=the_plan.kind, cell=cell, spec=spec, workdir=workdir,
                        base=base_commit, changed=so_far, risk=the_plan.risk, ledger=ledger,
                        corrections=corrections, launcher=launcher, store=store, task_id=task["id"], nth=nth,
                        say=say, outgrew=outgrew, registry=registry)
            verifier_s = time.monotonic() - mark
            timing["worker_s"] += worker_s
            timing["check_s"] += check_s
            timing["verifier_s"] += verifier_s
            if result.passed and deciding:
                settled = ledger_mod.outcome(deciding, result.verdicts)
                store.add_event(task["id"], "ledger_decided", seq=nth, **settled.data())
                if settled.stop:
                    review_stop = stops.Stop("review", settled.stop, attempt=nth, account=clip(result.summary, 2000))
            changed.update(result.changed_paths)
            # What checked the work this attempt left: cauce's check, or only the worker's own.
            report.checked = ("cauce" if (result.passed and options.verify) or stands == "cauce" else
                              "worker" if (result.passed and result.evidence.strip()) or stands == "worker" else "")
            report.cells.append(cell.label)
            report.served.append(result.served_model)
            report.cost_usd += result.cost_usd
            seq = store.add_attempt(
                task["id"], cell=cell.label, max_turns=turns, passed=int(result.passed),
                failure=result.failure.value if result.failure else None, summary=result.summary,
                evidence=kept_evidence(result.evidence), cost_usd=result.cost_usd,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens, turns=result.turns,
                duration_s=result.duration_s, changed_paths=result.changed_paths,
                served_model=result.served_model or None,
                capabilities=selection.names, session_id=session.id or None,
            )
            store.add_event(task["id"], "attempt_finished", seq=seq, cell=cell.label, passed=result.passed,
                            failure=result.failure.value if result.failure else None, cost_usd=result.cost_usd,
                            turns=result.turns, summary=result.summary[:500], denied=list(result.denied),
                            allow=list(result.allow), served_model=result.served_model,
                            permission_mode=result.permission_mode,
                            # Whether it went on in an earlier attempt's session; `fresh` says why one due did not.
                            continued=bool(spec.resume_session) and not result.fresh,
                            **({"fresh": result.fresh} if result.fresh else {}),
                            changed=list(result.changed_paths)[:50],
                            wall_s=round(worker_s, 2), duration_s=round(result.duration_s, 2),
                            check_s=round(check_s, 2), verifier_s=round(verifier_s, 2),
                            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                            cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
                            # The attempt's cost holds its independent check's; each is kept apart here.
                            worker_cost_usd=round(worker_cost, 4),
                            verifier_cost_usd=round(result.cost_usd - worker_cost, 4),
                            # What the dispatcher's level reads (`cauce.capacity`): the API, or the machine, said no.
                            **({"limited": result.limited, "retry_after_s": result.retry_after_s}
                               if result.limited else {}),
                            **({"starved": result.starved} if result.starved else {}),
                            **({"refused_but_written": list(result.overcome)} if result.overcome else {}))
            attempt = Attempt(cell, turns, result.passed, result.failure, result.summary, result.denied,
                              allow=result.allow, changed=result.changed_paths, evidence=result.evidence[-1500:],
                              transient=result.transient, findings=result.findings, network=result.network,
                              load=result.load, shape=result.shape, limited=result.limited,
                              retry_after_s=result.retry_after_s,
                              blockers=open_blockers, stands=bool(stands) and result.failure in REPLAN)
            attempts.append(attempt)
            if raced is not None and not raced_first:
                # The start's try went on: the rung's, stopped or failed, is recorded after it.
                store.set_move(task["id"], seq, "race", f"raced {raced.other_cell.label} beside it")
                _record_raced(store, task["id"], raced, turns, selection.names, report, first=False)
                if not result.passed:
                    attempts.append(_raced_attempt(raced, turns))
            subject = result.commit or subject
            if result.report is not None:
                store.add_event(task["id"], "report", seq=seq, passed=result.passed, report=result.report)
            say(f"cauce: #{task['id']} attempt {seq} " + ("passed" if result.passed else
                f"ended {(result.failure or Failure.INCONCLUSIVE).value}: {clip(result.summary, 160)}"))
            if review_stop is not None:
                report.stop = review_stop
                report.status, report.summary = "blocked", review_stop.reason
                break
            if result.passed:
                report.status, report.final_cell, report.summary = "done", cell.label, result.summary
                report.learned, report.parts, report.contract = result.learned, result.parts, result.contract
                report.criteria = result.criteria
                for adapter, status in the_plan.active:
                    report.impact += adapter.assess(result.changed_paths, repo_dir, status)
                if len(attempts) > 1:
                    _remember(store, task, key, attempt, "worked", result, kind=the_plan.kind,
                              role=role_name)
                if result.orientation:
                    cells.record_orientation(store, task["id"], result.orientation, role=role_name,
                                             areas=cells.of_paths(result.changed_paths) or reach)
                break
            if result.failure in _MEMORABLE and result.summary:
                _remember(store, task, key, attempt, "failed", result, kind=the_plan.kind,
                          role=role_name)
            reading = not writes or (declared_read_only and not any(a.changed for a in attempts))
            decision = decide(the_plan.ladder, attempts, allow_approval=options.allow_approval, reading=reading,
                              several=_several(the_plan, options, task))
            resume, fresh = _continues(decision, session)
            store.set_move(task["id"], seq, decision.move.value, decision.reason)
            to = decision.cell.label if decision.cell and decision.continues else None
            report.moves.append(f"{decision.move.value}" + (f" to {to}" if to else "") + f": {decision.reason}")
            report.decisions.append(decision)
            store.add_event(task["id"], "moved", seq=seq, move=decision.move.value, reason=decision.reason,
                            next_cell=decision.cell.label if decision.cell else None,
                            from_cell=cell.label, to_cell=to, axis=decision.axis,
                            trigger=(result.failure or Failure.INCONCLUSIVE).value,
                            because=list(decision.because), skipped=list(decision.skipped),
                            turns_from=turns, turns_to=decision.max_turns if decision.continues else None,
                            spent_usd=round(report.cost_usd, 4),
                            **(_continuation(decision, resume, fresh) if decision.continues else {}),
                            **({"wait_s": decision.wait_s, "network": attempt.network} if attempt.network else {}),
                            **({"wait_s": decision.wait_s, "load": attempt.load} if attempt.load else {}))
            if not decision.continues:
                # The worker's own account goes with the reason: "the task is
                # wrong" is only actionable next to what it found wrong.
                report.status = _FINAL_STATUS[decision.move]
                report.summary = f"{decision.reason}. The last worker's own account: {result.summary}" if (
                    result.summary) else decision.reason
                report.stop = stops.Stop(
                    _cause(decision, attempt), decision.reason, denied=attempt.denied, allow=attempt.allow,
                    attempt=seq,
                    next_cell=decision.cell.label if decision.cell else None,
                    account=clip(findings_mod.text(attempt.blockers) if decision.move is Move.REVIEW
                                 else result.summary, 2000),
                    extra={"checked": report.checked} if decision.move is Move.DECIDE
                    else _limit_extra(store, attempt) if _cause(decision, attempt) == "limit" else {})
                break
            if decision.move is Move.NEXT_MODEL and workspace is not None:
                # A new model starts from the base, not the bet that lost; what the
                # last one did is kept on a ref, and the brief says both.
                ref = isolate.set_aside(workspace, seq)
                isolate.reset(workspace)
                if joined and joined["merged"]:
                    # The base is where the parts began: the ones that joined cleanly are joined again.
                    isolate.join(workspace, joined["merged"], message=_join_message(task["id"], options))
                if ref:
                    aside.append(f"Before this attempt the worktree was reset to the base commit {workspace.base[:12]}"
                                 f": attempt {seq}'s changes ({cell.label}) are not in it. They are kept: "
                                 f"`git diff {workspace.base[:12]} {ref}` shows them. Take what is right from them, "
                                 "not their approach.")
            if decision.wait_s:
                mark = time.monotonic()
                _pause(store, task["id"], decision.wait_s, say,
                       f"the network dropped ({attempt.network}); trying {decision.cell.label} again"
                       if attempt.network else
                       f"the check's tests timed out under load ({attempt.load}); trying {decision.cell.label} again")
                timing["wait_s"] += time.monotonic() - mark
            cell, turns = decision.cell, decision.max_turns
    except Cancelled:
        report.stop = _cancelled(store, task["id"], len(attempts) or None)
        report.status, report.summary = "cancelled", report.stop.reason
    except KeyboardInterrupt:
        report.stop = stops.Stop("cancelled", "you cancelled it with Ctrl-C before it finished", by="you",
                                 attempt=len(attempts) or None, extra={"via": "keyboard"})
        report.status, report.summary = "cancelled", report.stop.reason
    finally:
        held.close()
        if workspace is not None:
            report.isolated = True
            report.changed = list(isolate.changed(workspace))
            # Work that outgrew its turns is kept too: the task was not wrong, only big. A
            # cancel stops the work, never throws it away: what it changed stays on its branch.
            # Nor does a resume that ends short drop what an earlier run of it kept.
            outgrew = report.stop is not None and report.stop.cause in ("turns", "split")
            report.branch = isolate.finish(
                workspace, keep=report.status in KEEPS_WORK or outgrew or report.status == "cancelled" or resumed,
                message=isolate.message(task["id"], title=commit_title(task), subject=subject,
                                        summary=attempts[-1].summary if attempts else "",
                                        status="done" if report.status == "done" else report.status,
                                        check_output=len([p for p in report.check_output if p in report.changed])),
            )
            if report.branch:
                with contextlib.suppress(isolate.IsolationError):
                    report.commit = isolate.head(workspace.repo_dir, report.branch)
            if report.branch and report.status == "done":
                # The fix that worked is anchored to the commit a person can check.
                store.set_fix_commit(task["id"], isolate.head(workspace.repo_dir, report.branch))
        status = report.status if report.status != "running" else "failed"
        if status != "done" and report.stop is None:
            # Nothing above ended it: an error escaped the loop, and cauce, not the work, failed.
            error = sys.exc_info()[1]
            report.stop = stops.Stop("crashed", f"cauce itself failed mid-run: {type(error).__name__}: {error}"[:500]
                                     if error else "it ended without a verdict", attempt=len(attempts) or None)
            report.summary = report.summary or report.stop.reason
        store.update_task(task["id"], status=status, final_cell=report.final_cell, result=report.summary,
                          pid=None, current_cell=None)
        if status == "done":
            # Its earlier copies that stopped waited on this work; it is done.
            report.superseded = stops.supersede(store, store.get_task(task["id"]))
            # A resumed task that had paused its lane passed: the queue behind it goes on.
            if store.release_lane(key, task["id"]) or report.superseded:
                dispatch.reopened(task, os.environ)
        # A person's cancel is their call on this task, not a broken state the
        # next one would start on: the queue behind it goes on.
        by_a_person = report.stop is not None and report.stop.cause == "cancelled" and report.stop.who == "you"
        # An investigator's stop is no broken state of the project: the person's queue goes on.
        reading_for_init = '"investigation"' in (store.get_task(task["id"]).get("options") or "")
        if status != "done" and store.get_task(task["id"])["dispatched"] and not by_a_person and not reading_for_init:
            store.pause_lane(key, f"task #{task['id']} ended {status}: {clip(report.summary, 200, one_line=True)}")
        if workspace is None:
            report.changed = sorted(changed)
        mark = time.monotonic()
        if status == "done" and report.learned and options.keep_learned and config.enabled(
                "notes", os.environ, options.notes):
            _note(store, task, key, report, the_plan, workspace.repo_dir if workspace else repo_dir,
                  isolate.head(workspace.repo_dir, report.branch) if workspace and report.branch else None,
                  notes_ask)
        if status == "done" and report.learned and options.keep_learned and config.enabled(
                "engram_write", os.environ):
            _to_engram(store, task, report, the_plan, workspace.repo_dir if workspace else repo_dir)
        # Filing what it learned runs before the ending and the next workflow step: timed.
        timing["notes_s"] = time.monotonic() - mark
        if status == "done":
            # A security problem the worker named is an alert of its own, never only a note among others.
            report.alerts = notes.security_alerts(report.learned, attempts[-1].findings if attempts else ())
        size = _realized(store, task["id"], report, workspace, time.monotonic() - clock)
        store.add_event(task["id"], "finished", status=status, final_cell=report.final_cell,
                        cost_usd=round(report.cost_usd, 4), branch=report.branch, impact=report.impact,
                        tip=_tip(workspace, report.branch),
                        commit=report.commit, checked=report.checked, isolated=report.isolated,
                        alerts=report.alerts,
                        changed=report.changed[:CHANGED_KEPT], changed_n=len(report.changed),
                        stop=report.stop.data() if report.stop else None,
                        risk=the_plan.risk.data(), points=the_plan.points.points, realized=size.points,
                        realized_why=list(size.reasons),
                        findings=list(attempts[-1].findings)[:30] if status == "done" and attempts else [],
                        wall_s=round(time.monotonic() - clock, 2), attempts=len(attempts),
                        at=round(time.time(), 3), **gaps,
                        **{k: round(v, 2) for k, v in timing.items()},
                        **({"ledger": settled.data()} if settled else {}),
                        **({"verdict": verdict_card(store, task["id"], report, options, verifying)}
                           if status == "done" else {}),
                        **({"parts": list(report.parts), "contract": report.contract}
                           if status == "done" and report.parts else {}))
        store.add_message(task["id"], "worker", report.text())
        # The next step, and the work queued after this one, start here, from the engine:
        # no session relays them. Advancing never fails the run that just ended, and never silently.
        try:
            workflows.advance(store, store.get_task(task["id"]), os.environ)
        except Exception as exc:
            store.add_event(task["id"], "workflow_error", error=str(exc)[:300])
            say(f"cauce: #{task['id']} could not start the work that waits on it: {exc}")
    return report


#: The most of one criterion's evidence a verdict keeps.
CRITERION_EVIDENCE = 240


def verdict_card(store: Store, task_id: int, report: Report, options: Options, verifying: bool) -> dict[str, Any]:
    """What the passing work was held to, kept with its ending so the session that sent it
    reads the verdict instead of re-reading the code: each criterion `S#` the worker
    answered with its evidence, the repository's check that cauce ran (never the worker's
    word), the independent check's outcome or why it did not run, and how many attempts it
    took. Read from cauce's records and git only."""
    verified = store.last_event(task_id, "verified")
    if verified is not None:
        data = verified["data"] or {}
        independent = {"ran": True, "passed": bool(data.get("passed")), "blocking": int(data.get("blocking") or 0)}
    elif options.reviewed_next:
        independent = {"ran": False, "why": "a blind review step of this workflow reads the change next"}
    elif not verifying:
        independent = {"ran": False, "why": "it changed nothing to check, or the verifier is off"}
    else:
        independent = {"ran": False, "why": f"its risk ({report.plan.risk.tier}) does not call for one"}
    return {
        "criteria": [{"id": str(c.get("id")), "met": bool(c.get("met")),
                      "evidence": " ".join(str(c.get("evidence") or "").split())[:CRITERION_EVIDENCE]}
                     for c in report.criteria if isinstance(c, Mapping)],
        "check": _check_card(options.verify, report.preexisting) if options.verify else None,
        "independent": independent,
        "attempts": len(report.cells),
    }


#: What a verdict card says of a check that fails only as it does at the base.
FAILING_AT_BASE = "failing at base, unchanged"


def _check_card(command: str, found: Mapping[str, Any] | None) -> dict[str, Any]:
    """The verdict card's check: passed, or failing at the base too with the same failures
    (`Baseline.found`): it stood as a pass, and the card says which it was."""
    if not found:
        return {"command": command, "passed": True}
    return {"command": command, "passed": True, "state": FAILING_AT_BASE, "base": str(found.get("base") or ""),
            "failures": int(found.get("n") or 0), "fixed": int(found.get("fixed") or 0)}


_STEP_TAG = re.compile(r"^\[[\w.-]+#\d+ [\w.-]+\]\s*")
_LEAD = re.compile(r"^[A-Z][^:\n]{0,40}:\s+")


def _tip(workspace: isolate.Workspace | None, branch: str | None) -> str | None:
    """The commit a kept branch ends on, kept with the ending: what builds on it can still
    find the work if the branch is ever deleted."""
    if workspace is None or not branch:
        return None
    try:
        return isolate.head(workspace.repo_dir, f"refs/heads/{branch}")
    except (isolate.IsolationError, OSError):
        return None


def commit_title(task: Mapping[str, Any]) -> str:
    """The fallback subject of a kept commit when the worker proposed none: the task's
    title, except for a workflow step, whose title is the step's ("[feature#19 build]
    Build it, with its tests"): its change is its goal, the first line of its text
    without the step's lead-in ("Build this feature: ")."""
    title = str(task.get("title") or "")
    if not _STEP_TAG.match(title):
        return title
    first = next((line.strip() for line in str(task.get("body") or "").splitlines() if line.strip()), "")
    return _LEAD.sub("", first) or _STEP_TAG.sub("", title)


def _realized(store: Store, task_id: int, report: Report, workspace: isolate.Workspace | None,
              wall_s: float) -> points_mod.Score:
    """The size the task turned out to be, from what git and its attempts measured: the files it
    changed, the lines its kept branch holds (none counted without one), every attempt's turns,
    and the wall time of this run and the ones before it."""
    attempts = store.attempts(task_id)
    lines = None
    if workspace is not None and report.branch:
        counted = isolate.numstat(workspace.repo_dir, workspace.base, report.branch)
        lines = sum(n for n, _ in counted) if counted is not None else None
    before = sum(float(e["data"].get("wall_s") or 0) for e in store.events_of(task_id, "finished"))
    return points_mod.realized(files=len(report.changed), lines=lines,
                               turns=sum(int(a["turns"] or 0) for a in attempts),
                               minutes=(before + wall_s) / 60, attempts=len(attempts))


def _join_message(task_id: int, options: Options) -> Callable[[str], str]:
    """The merge commit of each part an integrate step joins: the part it brings, as the history reads it."""
    names = {b["branch"]: b.get("part") or b["branch"] for b in (options.integrate or {}).get("branches") or ()}
    return lambda branch: isolate.message(task_id, title=f"Join the {names.get(branch, branch)} part",
                                          summary=f"Merges {branch}, the part the plan named {names.get(branch)}.")


def _integrate(store: Store, task_id: int, workspace: isolate.Workspace | None, options: Options, report: Report,
               context: list[str], say: Callable[[str], None],
               baseline: Baseline | None = None) -> tuple[dict[str, Any] | None, bool]:
    """Join an integrate step's branches in its worktree, in the plan's order. A clean join
    whose check (the run's `--verify`) passes, or that has none, passes with no worker; a
    conflict, or a check that fails on the joined work, goes to one, told what and why.
    (what was joined, whether a worker must go on); `report` holds the ending otherwise."""
    spec = options.integrate or {}
    branches = [b["branch"] for b in spec.get("branches") or () if b.get("branch")]
    names = {b["branch"]: b.get("part") or b["branch"] for b in spec.get("branches") or ()}
    if workspace is None:
        report.stop = stops.Stop("environment", "an integrate step joins the parts' branches in a worktree of its "
                                 "own, and this run has none (not a git checkout, or isolation is off)")
        report.status, report.summary = "blocked", report.stop.reason
        return None, False
    try:
        joined = isolate.join(workspace, branches, message=_join_message(task_id, options))
    except isolate.IsolationError as exc:
        report.stop = stops.Stop("environment", f"could not join the parts: {exc}"[:500])
        report.status, report.summary = "blocked", report.stop.reason
        return None, False
    store.add_event(task_id, "integrated", merged=joined["merged"], conflict=joined["conflict"],
                    files=joined["files"])
    clean = ", ".join(names[b] for b in joined["merged"]) or "nothing"
    contract = (("\n\nThe contract every part keeps, from the plan, verbatim (context: keep it):\n"
                 + "\n".join(f"> {line}" for line in spec["contract"].splitlines())) if spec.get("contract") else "")
    if joined["conflict"]:
        branch = joined["conflict"]
        rest = " then ".join(f"`git merge --no-edit {b}`" for b in joined["left"][1:])
        say(f"cauce: #{task_id} joining {names[branch]} conflicts in {', '.join(joined['files'][:5])}")
        context.append(
            f"Joining the parts in the plan's order: {clean} merged cleanly. Merging `{branch}` (the {names[branch]} "
            f"part) conflicts in: {', '.join(joined['files'])}. Run `git merge --no-edit {branch}` here, resolve "
            "those files so the parts work together and keep the contract, and commit the merge"
            + (f"; then merge the rest in this order, resolving each the same way: {rest}." if rest else ".")
            + contract)
        return joined, True
    summary = f"joined {len(joined['merged'])} parts cleanly ({clean})"
    if not options.verify:
        report.status, report.summary = "done", summary + "; no check was given: the review that follows reads it"
        return joined, False
    code, full = _steady_check(options.verify, workspace.path, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if code == 0:
        report.status, report.summary = "done", f"{summary}; `{options.verify}` passed on the joined work"
        return joined, False
    if (why := cannot_run(code, output)) is not None:
        report.stop = stops.Stop("environment", f"{summary}, but `{options.verify}` could not run here ({why})")
        report.status, report.summary = "blocked", report.stop.reason
        return joined, False
    before, compared = _against_base(baseline, options.verify, full, workspace.path) \
        if _ran_and_failed(code, full) else (False, "")
    if before:
        report.preexisting = baseline.found if baseline is not None else None
        report.status, report.summary = "done", (f"{summary}; `{options.verify}` fails on the joined work only as it "
                                                 "does at the base")
        return joined, False
    context.append(f"The parts merged cleanly ({clean}), but the run's check `{options.verify}` fails on the joined "
                   "work. Make it pass where the parts meet, keeping the contract; change what a part does only as "
                   f"far as that needs. Its output:\n{output[-1500:]}" + (f"\n\n{compared}" if compared else "")
                   + contract)
    return joined, True


#: Words that say a task needs a browser, or servers that keep running.
_BROWSER = re.compile(r"\b(browser|navegador|navigate|navega\w*|abr[ie]\w* (?:el )?navegador|screenshot|"
                      r"captura de pantalla|playwright|puppeteer|e2e)\b", re.IGNORECASE)
_SERVER = re.compile(r"(\bdev servers?\b|\blevanta\w*\b|\bstart (?:both |the )?(?:dev )?servers?\b|"
                     r"\bng serve\b|\bnpm (?:run )?(?:start|dev)\b|localhost:\d+)", re.IGNORECASE)
_BROWSER_CAPS = ("browser", "playwright", "chrome", "puppeteer", "rastro")
#: A task that tells the worker to start its servers: it needs them, whatever its check, and
#: its worker starts them itself.
_START_SERVERS = re.compile(
    r"(\blevanta\w*\b|\barranca\w*\s+(?:el|la|los|las|ambos)\b|"
    r"\binicia\w*\s+(?:el |la |los |las )?(?:servidor\w*|servicios?|backend|front\w*|api|app)\b|"
    r"\b(?:start|run|spin up|bring up|boot)\s+(?:up\s+)?(?:both |the |all |your |its )?(?:own )?(?:dev |local )?"
    r"(?:servers?|backend|back-?end|frontend|front-?end|front)\b"
    r"(?!\s+(?:tests?|checks?|suites?|specs?|lint\w*|builds?|unit\w*|pruebas?)\b))", re.IGNORECASE)
#: A check that talks to a running server: only then does a task's mention of one
#: say the check needs it. Lint, unit tests and a build do not.
_CHECK_SERVES = re.compile(r"(\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b|\be2e|playwright|cypress|puppeteer|\bcurl\b|"
                           r"\bwget\b|lighthouse|wait-on|start-server-and-test)", re.IGNORECASE)
#: A permission a check is run with (`--allow-net=localhost:8000`): no server it talks to.
_CHECK_GRANTS = re.compile(r"--allow-[\w-]+=\S*", re.IGNORECASE)


#: A task that says in its own words it must change nothing: a validation whose
#: findings are its answer, whatever kind it was filed under. The words must be
#: about the task: "a read-only version history" is a thing to build, and "do not
#: edit any file under src/admin" bounds a change instead of forbidding one.
_READ_ONLY = re.compile(
    r"\bread[- ]only(?:\s*[:)]|\s+(?:validation|audit|review|investigation|check|pass|run|session|task)\b)"
    r"|(?:tarea|validaci[oó]n|auditor[ií]a|revisi[oó]n|investigaci[oó]n|sesi[oó]n)\s+(?:de\s+)?s[oó]lo\s+"
    r"(?:de\s+)?lectura|[,(:]\s*s[oó]lo\s+(?:de\s+)?lectura\s*(?:[).,;\n]|$)"
    r"|\bdo not (?:edit|modify|change|touch)\b[\w ,]{0,25}?\bany (?:repo |repository )?files?\b"
    r"(?!\s+(?:under|inside|outside|other|except|besides|but|in\s+(?!any\b|the\s+repo|this\s+repo)))"
    r"|\bno (?:edites|modifiques|cambies|toques)\b[\w ,]{0,25}?\b(?:ning[uú]n\s+)?archivos?\b"
    r"(?!\s+(?:de|en|bajo|dentro|fuera|salvo|excepto|que)\b)",
    re.IGNORECASE)
#: What a task quotes or puts in parentheses is usually evidence about something else (what another
#: task did: "read-only", (a read-only review)). A parenthesis that opens with the declaration is the
#: task's own, whatever follows it: "Investigate (read-only) a timezone bug", "(read-only: report the
#: cause)", and the same in Spanish ("solo lectura").
_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d|`[^`\n]*`')
_ASIDE = re.compile(r"\((?!\s*(?:read[- ]only|s[oó]lo\s+(?:de\s+)?lectura)\s*[:;,)])[^()\n]*\)", re.IGNORECASE)
#: Kinds whose work is the change itself: whatever their text says is read-only,
#: they write. Every misreading of a task as read-only was one of these.
_BUILDS = frozenset({"feature", "implement", "refactor", "docs"})


def declares_read_only(kind: str | None, text: str) -> bool:
    """Whether the task says in its own words that it changes nothing. A build kind never does, and what
    the text quotes or puts in an aside describes something else, so it is not read."""
    if kind in _BUILDS:
        return False
    return bool(_READ_ONLY.search(_ASIDE.sub(" ", _QUOTED.sub(" ", text))))


_PORT = re.compile(r"(?:localhost|127\.0\.0\.1):(\d{2,5})\b")


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _all_listening(text: str) -> bool:
    """A task that names its local servers by port, every one of them up: the
    servers it needs are running, and saying otherwise is noise."""
    ports = {int(p) for p in _PORT.findall(text) if 0 < int(p) < 65536}
    return bool(ports) and all(_listening(p) for p in ports)


def _unmet(text: str, registry: Mapping[str, caps.Capability], *, served: bool = False,
           verify: str | None = None, where: Path | None = None) -> list[str]:
    """What a task asks for that a one-shot worker does not have, said before any
    money is spent: a worker has no browser unless a capability gives it one, or
    its check or the project's own tests drive a headless one (`ci.browser`), and
    a server it starts ends with it. A task that tells its worker to start its servers
    is said to have them started by its worker, never sent to `--service`; a task whose
    check needs no server (lint, tests, a build) is not told to start one for a word in
    its text, a host and port it names as context included."""
    notes = []
    if _BROWSER.search(text) and not any(k in name.lower() for name in registry for k in _BROWSER_CAPS):
        driven = ci.browser(where, verify) if where is not None else None
        if driven:
            notes.append(f"the browser this task needs is driven headless: {driven}")
        else:
            notes.append("this task needs a browser and workers have none: register a browser MCP server in "
                         "your cauce capabilities.json (`cauce capabilities --example` has one) and allow its "
                         "tools (--allow 'mcp__browser'), or check the pages yourself")
    mention, started = _SERVER.search(text), _START_SERVERS.search(text)
    if started and not served:
        notes.append(f"this task tells its worker to start its servers ({started.group(0).strip()!r}): the worker "
                     "starts them itself, and they stop when it finishes")
    elif mention and not served and not _all_listening(text) and (
            not verify or _CHECK_SERVES.search(_CHECK_GRANTS.sub(" ", verify))):
        notes.append(f"this task mentions {mention.group(0)!r}: if its work needs running servers, pass them with "
                     "--service 'npm run dev@4200' (cauce starts each before the work and stops it after), or "
                     "start them yourself before dispatching; a server a worker starts stops when it finishes")
    return notes


def _beside_ci(repo_dir: Path, check: str, services: Sequence[str], quieted: Collection[str] = ()) -> list[str]:
    """The jobs of the repository's CI the task's check leaves out, in one note (a warning:
    a pass here is not CI's), and the workflow files that could not be read, said once.
    Never a verdict and never a blocker; a job the person quieted here is not named."""
    note, unreadable = ci.left_out(repo_dir, check, services, quieted)
    lines = [note] if note else []
    if unreadable:
        lines.append(f"CI: {', '.join(unreadable)} could not be read; it is left out of what this check is "
                     "compared with")
    return lines


def _continues(decision: Decision, session: WorkerSession | None) -> tuple[str, str]:
    """The session the next attempt continues, and, when one was due and it starts fresh,
    why. More turns on the same cell continue it; more effort on the same model continues
    a fork of it at the new effort, since the approach stood and the depth was missing.
    Any other move is a new bet: a new model must not inherit the old one's approach, and
    a retry is for a launch that broke, unless only its answer's form failed: then the same
    cell answers again in its own session (`Decision.reshape`). A worktree is reset to the base only for another
    model (and undone outside its surfaces only as a wrong approach), so the model tells it."""
    if session is None or decision.cell is None or not (
            decision.move in (Move.MORE_TURNS, Move.MORE_EFFORT) or decision.reshape):
        return "", ""
    before, model = session.attempt, models.pinned(decision.cell.model) or decision.cell.model
    if not session.id:
        return "", f"attempt {before} kept no session to continue"
    if session.model != model:
        return "", f"attempt {before} ran on {session.model}, this one runs on {model}"
    if (decision.move is Move.MORE_TURNS or decision.reshape) and session.cell != decision.cell:
        return "", f"attempt {before} ran at {session.cell.label}, this one at {decision.cell.label}"
    return session.id, ""


def _continuation(decision: Decision, resume: str, fresh: str) -> dict[str, object]:
    """What a `moved` event says of the next attempt's session: continued or not, the
    session it goes on in (a fork for more effort), and why one due starts fresh. The
    attempt's own `attempt_finished` says whether it did (`fresh`: the CLI refused it)."""
    out: dict[str, object] = {"continued": bool(resume)}
    if resume:
        out["forked_from" if decision.move is Move.MORE_EFFORT else "resumed"] = resume
    if fresh:
        out["fresh"] = fresh
    return out


def _spans(repo_dir: Path, add_dirs: Sequence[str]) -> str | None:
    """A task that reaches a sibling repository (`--add-dir`) outside the workspace
    of its own: said once, with the command that makes them one project. Never done
    for the person: a workspace moves what each repository kept."""
    from cauce import workspaces

    own = repo.toplevel(repo_dir)
    tops = [t for t in (repo.toplevel(Path(d).expanduser()) for d in add_dirs) if t is not None and t != own]
    if own is None or not tops:
        return None
    folder = workspaces.of(own)
    apart = [t for t in tops if folder is None or workspaces.of(t) != folder]
    if not apart:
        return None
    named = [own, *apart] if folder is None else apart
    spans = (f"this task spans {len(apart) + 1} repositories ({', '.join(t.name for t in [own, *apart])}): tracked "
             "apart, each keeps its own notes and board")
    choice = ("making them one project is the person's choice, never cauce's: it moves what each repository kept "
              "(notes, tasks, rules) to that folder")
    if folder is None:
        # Only a folder that exists and holds exactly these: a workspace's folder holds only
        # its own repositories, and a path nobody made is no offer.
        parent = own.parent
        try:
            held = {c.resolve() for c in parent.iterdir() if (c / ".git").exists()}
        except OSError:
            held = set()
        if held != {t.resolve() for t in named} or parent == Path.home().resolve() or parent == Path("/"):
            return (f"{spans}; a workspace needs a folder that holds just them, which the person makes (or "
                    f"links them into) before `cauce workspace add <folder> --with …`; {choice}")
        folder = parent
    flags = " ".join(f"--with {shlex.quote(str(t))}" for t in named)
    return (f"{spans}; `cauce workspace add {shlex.quote(str(folder))} {flags}` would make them one project, "
            f"tracked in that folder; {choice}")


#: The repository's latest attempts a refusal is read from: an older one is history, not a warning.
REFUSALS_WINDOW = 40
#: The tasks a rule must have been refused in, within that window, before a plan names it.
REFUSED_IN_TASKS = 2


def _refused_here(store: Store, key: str | None, granted: Sequence[str], mode: str) -> list[tuple[str, int]]:
    """The rules workers in this repository lacked most often lately, past refusals read
    back to rules when an older run did not record them, the granted ones aside.

    Only refusals among the repository's latest `REFUSALS_WINDOW` attempts, of a rule
    refused in `REFUSED_IN_TASKS` tasks or more: one task's refusal is that task's, and
    is not repeated to every task after it. Only in the mode the next worker runs in:
    what `acceptEdits` refused, auto mode may well let through. An attempt that did not
    record its mode ran before auto mode, under `acceptEdits`."""
    if not key:
        return []
    counts: dict[str, int] = {}
    tasks: dict[str, set[int]] = {}
    for row in store.refusals(key, limit=REFUSALS_WINDOW):
        if (row.get("mode") or grants.FALLBACK_MODE) != mode:
            continue
        for rule in dict.fromkeys(row["allow"] or allow_rules.from_refusals(row["denied"])):
            if rule not in granted:
                counts[rule] = counts.get(rule, 0) + 1
                tasks.setdefault(rule, set()).add(int(row["task_id"]))
    shown = {rule: n for rule, n in counts.items() if len(tasks[rule]) >= REFUSED_IN_TASKS}
    return sorted(shown.items(), key=lambda kv: (-kv[1], kv[0]))[:8]


#: A task the plan sizes at this many points or more is several workers' work: out of turns,
#: it is split (`cauce split`) instead of given more (`escalate.decide`'s `several`).
SPLIT_POINTS = 8


def _several(the_plan: Plan, options: Options, task: Mapping[str, Any]) -> str:
    """Why the plan sized this task as several workers' work, or "": a build of
    `SPLIT_POINTS` or more that the session did not send `--whole`, outside a workflow run
    (a step's parts are its plan's) and never a reading kind."""
    if (options.whole or task.get("workflow_run") or the_plan.kind in READ_ONLY_KINDS or options.parts
            or options.integrate or options.ledger or the_plan.points.points < SPLIT_POINTS):
        return ""
    return f"{the_plan.points.points} points: " + ", ".join(the_plan.points.reasons[:3])


def _limit_extra(store: Store, attempt: Attempt) -> dict[str, Any]:
    """A stop for the account's usage limit: when the limit resets, when the message named it,
    kept on the stop for the resume after it and on the store's meta for the dispatcher."""
    if attempt.retry_after_s is None:
        return {}
    at = (datetime.now(UTC) + timedelta(seconds=float(attempt.retry_after_s))).isoformat(timespec="seconds")
    with contextlib.suppress(sqlite3.Error):
        if (store.meta(stops.LIMIT_META) or "") < at:
            store.set_meta(stops.LIMIT_META, at)
    return {"resets_at": at}


def _cause(decision: Decision, attempt: Attempt) -> str:
    """What ended the run, from the move and the failure that led to it."""
    if decision.move is Move.BLOCKED:
        if attempt.failure is Failure.PERMISSION or attempt.denied:
            return "permission"
        if attempt.limited == "usage" and attempt.failure is Failure.ENVIRONMENT:
            return "limit"
        return "network" if attempt.network else "load" if attempt.load else "environment"
    if decision.move is Move.NEEDS_APPROVAL:
        return "approval"
    if decision.move is Move.CONVERGED:
        return "converged"
    if decision.move is Move.REVIEW:
        return "review"
    if decision.move is Move.DECIDE:
        return "decision"
    if decision.move is Move.REPLAN:
        if attempt.failure in (Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG):
            return "spec"
        if decision.split:
            return "split"
        return "turns" if attempt.failure is Failure.TURNS_EXHAUSTED else "ladder"
    return "exhausted"


def _check_cancel(store: Store, task_id: int) -> None:
    if store.cancel_requested(task_id):
        raise Cancelled


#: What waits between tries; a test replaces it.
_sleep = time.sleep


def _pause(store: Store, task_id: int, seconds: int, say: Callable[[str], None], why: str) -> None:
    """Wait before the next try, said once, and stop at once for a cancel: a wait
    that says nothing reads as a run that hung."""
    say(f"cauce: #{task_id} {why} in {seconds}s")
    store.add_event(task_id, "waiting", reason=why, seconds=seconds)
    left = float(seconds)
    while left > 0:
        _check_cancel(store, task_id)
        _sleep(min(left, 2.0))
        left -= 2.0
    _check_cancel(store, task_id)


def _waits(store: Store, task_id: int, who: str, say: Callable[[str], None]) -> None:
    """Said once, when a run finds its checkout taken: a wait that says nothing
    reads as a run that hung."""
    say(f"cauce: #{task_id} waits: {who} is working in this checkout without a worktree; to work beside it, "
        "run it in its own worktree (without --no-isolate) — a wait is not a hang, do not cancel it for one")
    store.add_event(task_id, "waiting", reason=f"{who} is working in this checkout")


def _cancelled(store: Store, task_id: int, attempt: int | None) -> stops.Stop:
    """A cancel a person asked for says how they asked; a SIGTERM nobody asked
    cauce for (a closed terminal, a killed process) says that instead."""
    if store.cancel_requested(task_id):
        asked = store.last_event(task_id, "cancel_requested")
        via = str(((asked or {}).get("data") or {}).get("via") or "cli")
        how = stops.CANCEL_VIA.get(via, via)
        return stops.Stop("cancelled", f"you cancelled it {how} before it finished", by="you", attempt=attempt,
                          extra={"via": via})
    return stops.Stop("signal", "a SIGTERM that did not come from `cauce cancel` or the UI stopped it "
                      "(a closed terminal, or the process was killed)", attempt=attempt)


def how_to_run(workdir: Path, rules: Sequence[str]) -> list[str]:
    """Brief lines on running the project's tools: the Node already on the worker's
    PATH, and the commands its settings let through, in the forms that pass. Workers
    that sourced a version manager or wrapped a command in a shell were refused."""
    lines = []
    node = runtime.node_bin(workdir)
    if node is not None:
        lines.append(f"Node {node.version} (what {node.source} asks for) is already first on your PATH: run "
                     "`npm test`, `npx tsc` and the project's scripts by name. Never `source ~/.nvm/nvm.sh`, "
                     "`nvm use` or `bash -c '…'`: they are refused, and a refused command stops the task.")
    allowed = [r for r in rules if r.startswith("Bash(")]
    if allowed:
        lines.append("Shell commands your settings let through without asking, in these forms only, one per "
                     "call: " + ", ".join(dict.fromkeys(allowed[:20])))
    return lines


#: Fewer corrections than this a verifier's findings may earn; then a person decides.
MAX_CORRECTIONS = 2


def _gaps(store: Store, task_id: int, began: float) -> dict[str, float]:
    """What came before a dispatched run of this task: `advance_s` (from the step it waited
    on passing to its being queued), `pickup_s` (from queued, or released from behind other
    work, to the dispatcher starting it) and `spawn_s` (from that to this run's start). Only
    what this run's own events measured, since its last ending; nothing is guessed."""
    ended = store.last_event(task_id, "finished")
    since = ended["id"] if ended else 0
    out: dict[str, float] = {}
    advanced = store.last_event(task_id, "advanced")
    took = ((advanced or {}).get("data") or {}).get("advance_s")
    if advanced and advanced["id"] > since and isinstance(took, int | float):
        out["advance_s"] = round(float(took), 3)
    dispatched = store.last_event(task_id, "dispatched")
    at = ((dispatched or {}).get("data") or {}).get("at")
    if not dispatched or dispatched["id"] <= since or not isinstance(at, int | float):
        return out
    out["spawn_s"] = round(max(began - at, 0.0), 3)
    released = store.last_event(task_id, "released")
    task = store.get_task(task_id) or {}
    queued = _epoch(released["ts"] if released and released["id"] > since else task.get("created_at"))
    if queued is not None:
        out["pickup_s"] = round(max(at - queued, 0.0), 3)
    return out


def _epoch(stamp: str | None) -> float | None:
    """A stored timestamp as seconds since the epoch; None when it cannot be read."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return (then if then.tzinfo else then.replace(tzinfo=UTC)).timestamp()


def _seconds_since(stamp: str | None) -> float | None:
    """Seconds from a stored timestamp to now; None when it cannot be read."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return round(max((datetime.now(UTC) - then).total_seconds(), 0.0), 1)


def _session_context(task: Mapping[str, object], text: str) -> tuple[list[str], dict[str, int] | None]:
    """The part of the sending session's thread this task needs, as the scribe kept
    it, read up to now (the turn that sent it is not over). An investigator gets
    none: its brief is the survey. Reading it never fails the run."""
    session_id = str(task.get("session_id") or "")
    if not session_id or '"investigation"' in str(task.get("options") or "") or not config.enabled(
            "scribe", os.environ):
        return [], None
    from cauce import scribe

    try:
        state = scribe.load(home(), session_id)
        transcript = (state or {}).get("transcript") or scribe.find_transcript(session_id)
        if transcript:
            state = scribe.update(home(), session_id, transcript) or state
        lines, got = scribe.context_for(state, text)
    except (OSError, ValueError, KeyError, TypeError):
        return [], None
    return lines, (got if lines else None)


def _twins(store: Store, task: Mapping[str, object], repo_dir: Path) -> list[int]:
    """Earlier tasks here with the same text that stopped and keep their work on a branch."""
    if repo.toplevel(repo_dir) is None:
        return []
    body = (task.get("body") or "").strip()
    return [t["id"] for t in store.list_tasks(repo=task.get("repo"), status=list(stops.DISMISSABLE),
                                              source=("cauce", "queue"), limit=200)
            if t["id"] != task["id"] and (t.get("body") or "").strip() == body and isolate.kept(repo_dir, t["id"])]


def _fill_worktree(store: Store, task_id: int, workspace: isolate.Workspace, text: str, carry: bool,
                   say: Callable[[str], None]) -> list[str]:
    """What a new worktree lacks that the task needs, from the person's checkout: their
    uncommitted changes (brought with `carry`, else warned about when the task names
    them), and the ignored paths the task names (copied). Brief lines for each."""
    lines = []
    dirty = isolate.dirty(workspace.repo_dir)
    named = isolate.named_in(text, dirty)
    if carry and dirty:
        carried = isolate.carry(workspace, dirty)
        store.add_event(task_id, "carried", paths=carried[:100])
        say(f"cauce: #{task_id} brought {len(carried)} uncommitted change(s) from your checkout into its worktree")
        lines.append("The person's uncommitted changes were brought into this worktree, as a starting point: "
                     + ", ".join(carried[:15]) + (f" and {len(carried) - 15} more" if len(carried) > 15 else ""))
    elif named:
        store.add_event(task_id, "uncommitted", paths=named[:100])
        say(f"cauce: #{task_id} warning: {', '.join(named[:5])} have uncommitted changes in your checkout that "
            "its worktree does not have; commit them, or run it with --carry to bring them")
        lines.append("These files have uncommitted changes in the person's checkout that are not in this worktree: "
                     + ", ".join(named[:15]) + ". If the task needs that work, say so (`environment`) rather than "
                     "recreating it.")
    copied, big = isolate.bring(workspace, isolate.ignored_named(workspace, text))
    if copied:
        lines.append("Copied from the person's checkout (ignored by git, so never committed): " + ", ".join(copied))
    if big:
        lines.append("Named by the task but ignored by git and too big to copy here: " + ", ".join(big)
                     + ". If the task needs them, say so (`environment`).")
    return lines


def _head(repo_dir: Path) -> str | None:
    try:
        return isolate.head(repo_dir, "HEAD")
    except isolate.IsolationError:
        return None


def _cheap(cell: Cell) -> bool:
    """A cell whose pass a medium-risk change does not take on its own word."""
    return cell.model == "haiku" or cell.effort == "low"


def verify_cell(cell: Cell) -> Cell:
    """The verifier runs on the work's model (never Haiku) at high effort. Its
    independence is a fresh session given the request verbatim, not effort: work
    that climbed to xhigh or max is checked at high, which halves the wait on the
    slowest tasks and finds what a probe shows as well."""
    if cell.model == "haiku":
        return Cell("sonnet", "high")
    if cell.effort is not None and cell.effort != "high":
        return Cell(cell.model, "high")
    return cell


#: The turns of one independent check: it reads a diff and runs probes, and a
#: worker that climbed to many turns does not hand it the same room.
VERIFIER_TURNS = 40
#: Kinds whose change must make a failure go away: the check shows it at the base, and gone.
_REPRODUCES = frozenset({"debug-repro", "debug-unclear"})


def kept_evidence(text: str, room: int = 4000) -> str:
    """What an attempt's record keeps of its evidence: its start and its end, so a
    resume's brief that quotes the last lines quotes the real ones."""
    if len(text) <= room:
        return text
    half = (room - 3) // 2
    return f"{text[:half]}\n…\n{text[-half:]}"


def verifier_brief(text: str, kind: str, workdir: Path, base: str | None, changed: Sequence[str],
                   known: Sequence[Mapping[str, str]] = (), why: Sequence[str] = ()) -> str:
    """What the verifier is given: the request in its words, the change, and why it is
    checked (the risk's reasons, in the router's words) — never the worker's account."""
    shown = ", ".join(changed[:20]) + (f" and {len(changed) - 20} more" if len(changed) > 20 else "")
    parts = [f"Check this change. The request it answers ({kind}), verbatim:\n{text}"]
    if listed := criteria_mod.brief(text):
        parts.append(listed)
    if why:
        parts.append("Why this check runs: " + "; ".join(why) + ". Probe those first.")
    if kind in _REPRODUCES and base:
        parts.append(f"This change fixes a failure: show it at the base commit {base[:12]} (`git show "
                     f"{base[:12]}:<path>`, or a temporary `git worktree add` of it outside the project that you "
                     "remove after; never `git stash`), then show it is gone on the change.")
    where = (f"The change is in {workdir}: `git diff {base}` shows it against the base commit {base}, the state "
             "before it." if base else f"The change is in {workdir}, uncommitted: `git diff` shows it.")
    parts.append(f"{where} Files it changed: {shown}.")
    if known:
        parts.append("The previous check found these; say for each whether it is fixed, and report any new "
                     "problem apart:\n" + findings_mod.text(known))
    parts.append("This check is read-only: change no file of the project.")
    return "\n\n".join(parts)


def _independent_check(result: launch.WorkerResult, *, text: str, kind: str, cell: Cell, spec: launch.LaunchSpec,
                       workdir: Path, base: str | None, changed: Sequence[str], risk: risk_mod.Risk,
                       ledger: dict[str, Mapping[str, str]], corrections: int, launcher: Launcher, store: Store,
                       task_id: int, nth: int, say: Callable[[str], None], outgrew: bool = False,
                       registry: Mapping[str, caps.Capability] | None = None
                       ) -> tuple[launch.WorkerResult, stops.Stop | None, dict[str, Mapping[str, str]], int,
                                  tuple[dict[str, str], ...]]:
    """A pass checked again by a worker that verifies the request, not the work.

    The first problems it finds earn one correction. A later round is earned by
    a problem that survived (until `MAX_CORRECTIONS`), or by progress: every
    problem of the round before fixed, and a new one a probe shows. A new problem
    found after that progress and only read from the code does not stop the work:
    the pass stands and carries it, for a person to read. Whatever is left at the
    cap stops the task for a person, its work kept. A check that could not decide
    leaves the pass standing, and says so. Work whose worker ran out of turns
    before its own check (`outgrew`) failed for room, not for a bug: the next
    attempt gets more turns on the same cell, never a stronger model."""
    role = roles.internal("verifier")
    vcell = verify_cell(cell)
    # It keeps only the capabilities that measure, the work's and any the change touches: what it reports,
    # it measures.
    measures = caps.measuring(registry or {})
    hints = {name: plugins.MEASURES.get(name) or measures[name].hint
             for name in [*spec.mcp_servers, *measures] if name in plugins.MEASURES or name in measures}
    measuring = {name: server for name, server in spec.mcp_servers.items() if name in hints}
    touched = caps.select({n: c for n, c in measures.items() if n not in measuring}, kind, workdir=workdir,
                          paths=changed, reading=True)
    measuring.update(touched.servers)
    hints = {name: hints[name] for name in measuring}
    check = dataclasses.replace(
        spec, prompt=verifier_brief(text, kind, workdir, base, changed, list(ledger.values()), risk.reasons),
        cell=vcell, max_turns=min(spec.max_turns, VERIFIER_TURNS),
        # Its own session, never the worker's: it reads the request and the diff, not the work's account.
        resume_session="", fork_session=False, resume_note="", persist_session=False,
        timeout_s=launch.timeout_for(min(spec.max_turns, VERIFIER_TURNS)),
        model_id=models.pinned(vcell.model), permission_mode=grants.mode_for(vcell.model),
        disallowed_tools=tuple(dict.fromkeys([*spec.disallowed_tools, *launch.WRITE_TOOLS])),
        append_system_prompt="\n\n".join([roles.brief(role).system_prompt,
                                           *(f"{name}: {hint}" for name, hint in hints.items() if hint)]),
        mcp_servers=measuring,
        allowed_tools=tuple(r for r in spec.allowed_tools
                            if not r.startswith("mcp__") or r.removeprefix("mcp__") in measuring)
        + tuple(f"mcp__{name}" for name in touched.servers))
    say(f"cauce: #{task_id} attempt {nth} passed; risk {risk.tier}: an independent check at {vcell.label}")
    mark = time.monotonic()
    verdict = launcher(check)
    wall_s = time.monotonic() - mark
    cost = result.cost_usd + verdict.cost_usd
    shown = findings_mod.read({"findings": list(verdict.findings)})
    blockers = findings_mod.blocking(shown)
    if not verdict.passed and not blockers and verdict.verdict == "fail":
        blockers = [{"where": "", "what": verdict.summary or "the check failed without a finding",
                     "evidence_class": "inferential", "cause": "unknown", "proof": ""}]
    decided = verdict.passed or bool(blockers)
    still = [f for f in blockers if any(findings_mod.same(f, g) for g in ledger.values())]
    # Progress: the round before had problems and none of them is left.
    progress = corrections > 0 and not still
    carried = [f for f in blockers if f["evidence_class"] != "deterministic"] if progress else []
    if carried:
        blockers = [f for f in blockers if f not in carried]
    store.add_event(task_id, "verified", seq=nth, cell=vcell.label, risk=risk.data(), verdict=verdict.verdict,
                    passed=not blockers and decided, findings=list(shown)[:20], blocking=len(blockers),
                    blockers=list(blockers)[:20],
                    carried=len(carried), cost_usd=verdict.cost_usd, summary=verdict.summary[:500],
                    round=corrections, wall_s=round(wall_s, 2), duration_s=round(verdict.duration_s, 2),
                    turns=verdict.turns)
    if not decided:
        say(f"cauce: #{task_id} the independent check could not decide; the pass stands")
        return dataclasses.replace(result, cost_usd=cost, evidence=result.evidence + "\n\n[independent check "
                                   f"inconclusive: {clip(verdict.summary, 300)}]"), None, ledger, corrections, ()
    if not blockers:
        if carried:
            say(f"cauce: #{task_id} the earlier problems are fixed; {len(carried)} new one(s) read from the code "
                "go with the pass")
            return dataclasses.replace(
                result, cost_usd=cost, findings=(*result.findings, *carried),
                evidence=result.evidence + "\n\n[independent check: the problems it found before are fixed. "
                f"New, read from the code and not shown by a probe, for a person to read:\n"
                f"{findings_mod.text(carried)[:1500]}]"), None, ledger, corrections, ()
        return dataclasses.replace(result, cost_usd=cost), None, ledger, corrections, ()
    found = findings_mod.text(blockers)
    failure = Failure.TURNS_EXHAUSTED if outgrew else Failure.CODE_BUG
    room = " (the worker had run out of turns before checking its own work)" if outgrew else ""
    if corrections == 0 or ((still or progress) and corrections < MAX_CORRECTIONS):
        if corrections == 0 or progress:
            ledger = {findings_mod.key(f): f for f in blockers}
        say(f"cauce: #{task_id} the independent check found {len(blockers)} problem(s): a correction")
        failed = dataclasses.replace(
            result, passed=False, failure=failure, cost_usd=cost,
            summary=f"the independent check found {len(blockers)} problem(s) this change caused{room}: "
                    f"{result.summary}",
            evidence=f"The independent check found:\n{found}\n\nIts probes:\n{verdict.evidence[-1500:]}")
        return failed, None, ledger, corrections + 1, tuple(blockers)
    why = (f"after {MAX_CORRECTIONS} corrections it still shows problems the earlier checks had not"
           if progress else f"the same problem survived {MAX_CORRECTIONS} corrections" if len(still) == len(blockers)
           else f"after {MAX_CORRECTIONS} corrections a problem it found before is still there, beside new ones")
    stop = stops.Stop("review", f"the independent check still finds problems; {why}", attempt=nth,
                      account=clip(found, 2000))
    failed = dataclasses.replace(result, passed=False, failure=failure, cost_usd=cost,
                                 summary=f"the independent check still finds problems{room}: {result.summary}",
                                 evidence=found)
    return failed, stop, ledger, corrections, tuple(blockers)


def brief(
    text: str,
    kind: str,
    workdir: Path,
    attempts: Sequence[Attempt],
    dead_ends: Sequence[dict],
    verify: str | None,
    writes: bool,
    context: Sequence[str] = (),
    surfaces: Sequence[str] = (),
) -> str:
    parts = [f"Task ({kind}):\n{text}", f"Work in: {workdir}"]
    if listed := criteria_mod.brief(text):
        parts.append(listed)
    if bounds := surfaces_mod.brief(surfaces):
        parts.append(bounds)
    if context:
        parts.append("\n".join(context))
    if not writes:
        parts.append("This task is read-only: find out and report. Do not change files. The answer is the "
                     "deliverable, whatever it says: when you checked what was asked, the verdict is `pass`, also "
                     "when what you found is broken, missing or failing. Put the answer in `summary`, each finding "
                     "in `findings` (where, what, how it is known, and its proof), and what shows them (file:line, "
                     "a command and its output) in `evidence`. `code_bug` is never about the "
                     "code you read: use `inconclusive` for something you could not check, and `fail` with "
                     "`spec_bug` when the task asks for a change, which you cannot make here.")
    if verify:
        parts.append(f"When you report a pass, it is checked by running: {verify}")
        if writes and (near := ci.related(verify, workdir)):
            files = "<the test files of what you changed>" if near[0] == "pytest" else "<the files you changed>"
            parts.append(f"While you work, run only the tests near your change ({near[1].format(files=files)}): "
                         f"the full `{verify}` is slow, and cauce runs it when you are done. Run it yourself once, "
                         "at the end, before you report.")
    if dead_ends:
        lines = ["Fixes already tried against similar problems, here or in another repository. "
                 "Do not repeat one without saying why it would work this time:"]
        for d in dead_ends:
            where = f"{d['repo']}, {_SCOPE_SAID[d['scope']]}" if d.get("scope") in _SCOPE_SAID else d["repo"]
            line = f"- ({where}) {d['problem']}: tried {clip(d['tried'], 200, one_line=True)}"
            if d["why"]:
                line += f"; failed because {clip(d['why'], 200, one_line=True)}"
            if d["worked_instead"]:
                line += f"; what worked: {clip(d['worked_instead'], 200, one_line=True)}"
            lines.append(line)
        parts.append("\n".join(lines))
    if attempts:
        lines = ["Previous attempts on this task — already tried, do not repeat:"]
        for i, a in enumerate(attempts, 1):
            lines.append(f"  attempt {i} [{a.cell.label}, {a.failure or 'no verdict'}] {clip(a.summary, 300)}")
            if a.denied:
                lines.append(f"    refused: {', '.join(a.denied[:5])}")
        if failed := failed_output(attempts[-1]):
            lines.append("  " + failed)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def continuation_note(last: Attempt) -> str:
    """What a continued session is told failed its attempt, which its context does not
    hold: the independent check's blockers in a correction round (its work passed its
    own check), else the output that failed it."""
    if last.shape:
        return f"Why your last answer could not be read: {last.shape}."
    if last.blockers:
        return ("The independent check, which reads only the request and the diff, found these problems in your "
                "work after it passed its own check:\n" + findings_mod.text(last.blockers)[:3000])
    return failed_output(last)


def failed_output(last: Attempt) -> str:
    """The output that failed an attempt, for the next one: a fresh brief and a session
    that continues alike, since a red check is evidence for whatever comes next."""
    if last.passed or not last.evidence.strip():
        return ""
    return "The output that failed the last attempt, last lines:\n" + last.evidence.strip()[-1500:]


def _equip(selection: caps.Selection, the_plan: Plan, repo_dir: Path, workdir: Path) -> caps.Selection:
    """The registry's selection plus every adopted neighbour, each with its hint.
    A registry entry of the same name wins: a person who configured it chose it."""
    servers = dict(selection.servers)
    hints = list(selection.hints)
    names = list(selection.names)
    for adapter, _ in the_plan.active:
        server = adapter.server()
        if server is None or adapter.name in servers:
            continue
        servers[adapter.name] = server
        hints.append(f"{adapter.name}: {adapter.hint(the_plan.kind, repo_dir, workdir)}")
        names.append(adapter.name)
    return caps.Selection(servers, tuple(hints), tuple(names), selection.read_dirs)


def _run_check(command: str, workdir: Path) -> tuple[int | None, str]:
    """The repository's own check: (exit code, or None on a timeout; its whole output)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir), capture_output=True, text=True,  # noqa: S602
                              timeout=VERIFY_TIMEOUT_S, check=False, env={**os.environ, **runtime.env_for(workdir)})
    except subprocess.TimeoutExpired:
        return None, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()


#: How much of a check's output an attempt's evidence keeps.
CHECK_OUTPUT_KEPT = 3000


#: Where a check's run at a base commit is kept (`Store.meta`), and for how long: a task's
#: later attempts, its verifier rounds and its sibling parts on the same base read it there.
BASE_RUN_META, BASE_RUN_KEPT_S = "check_base:", 24 * 3600
#: How many failing lines a `check_preexisting` event and an attempt's evidence name.
FAILURES_SHOWN = 10


@dataclass
class Baseline:
    """What a red check is compared with: the same command at the commit the task's work
    started from (`isolate.at_base`). `must_fix` is work whose job is to make a failure go
    away (`_REPRODUCES`): the same failures, untouched, are not its pass."""
    store: Store
    task_id: int
    repo_dir: Path
    base: str
    root: Path
    must_fix: bool = False
    #: The last check's account when its failures were all there at the base, else None.
    found: dict[str, Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _owner: int = field(default_factory=threading.get_ident, repr=False)

    @contextlib.contextmanager
    def _db(self):
        """The store, or one of its own on another thread (a race checks each try in its own)."""
        if threading.get_ident() == self._owner:
            yield self.store
            return
        other = Store(self.store.path)
        try:
            yield other
        finally:
            other.close()

    def at_base(self, command: str) -> dict[str, Any]:
        """The command's run at the base: {"code", "failures", "cannot"}, from the store's
        meta when it ran there in the last `BASE_RUN_KEPT_S`, else run now and kept. A run
        that timed out, could not run, lost the network or timed its tests out is `cannot`:
        it cannot tell what was there before."""
        digest = hashlib.sha256(f"{self.repo_dir}\0{self.base}\0{command}".encode()).hexdigest()[:32]
        key = BASE_RUN_META + digest
        with self._lock:
            with self._db() as db:
                kept = db.meta(key)
            if kept is not None and not _expired(kept):
                return json.loads(kept)
            try:
                with isolate.at_base(self.repo_dir, self.base, self.root) as path:
                    code, output = _run_check(command, path)
                    failures = launch.check_failures(output, roots=(path,))
            except (isolate.IsolationError, OSError) as exc:
                return {"code": None, "failures": [],
                        "cannot": clip(f"no worktree could be made at the base ({exc})", 300, one_line=True)}
            cannot = ("it timed out" if code is None else "" if code == 0 else
                      cannot_run(code, output) or launch.check_timed_out(output) or launch.check_offline(output))
            found = {"at": time.time(), "code": code, "failures": sorted(failures), "cannot": cannot}
            if not (code and (launch.check_timed_out(output) or launch.check_offline(output))):
                # A loaded machine or a dropped network is a moment's: the next try runs it again.
                with self._db() as db:
                    db.set_meta(key, json.dumps(found))
                    db.drop_meta([k for k, v in db.metas(BASE_RUN_META).items() if _expired(v)])
            return found


def _expired(kept: str) -> bool:
    """A base's run kept longer than `BASE_RUN_KEPT_S`, or that cannot be read."""
    try:
        return time.time() - float(json.loads(kept).get("at") or 0) >= BASE_RUN_KEPT_S
    except (ValueError, AttributeError, TypeError):
        return True


def _against_base(baseline: Baseline | None, command: str, output: str, workdir: Path) -> tuple[bool, str]:
    """Whether a check that ran and failed on the work failed the same way at the base
    (`launch.preexisting`), and what the next attempt reads of the comparison. Fails closed:
    when the base passes, could not run, or no failing line can be read from either output,
    the failure is the work's."""
    if baseline is None:
        return False, ""
    baseline.found = None
    work = launch.check_failures(output, roots=(workdir, baseline.repo_dir))
    if not work:
        return False, ("cauce could not read which tests or files failed from its output, so it cannot tell "
                       "whether they failed before this work: the failure is read as this work's")
    at = baseline.at_base(command)
    short = baseline.base[:12]
    if at.get("cannot"):
        return False, (f"at the base commit {short} the same check could not tell ({at['cannot']}): the failure "
                       "is read as this work's")
    if at.get("code") == 0:
        return False, f"at the base commit {short} the same check passes: every failure here is this work's"
    base = frozenset(at.get("failures") or ())
    new, old = sorted(work - base), sorted(work & base)

    def listed(lines: Sequence[str]) -> str:
        more = len(lines) - FAILURES_SHOWN
        return "\n".join(f"  - {clip(x, 200, one_line=True)}" for x in lines[:FAILURES_SHOWN]) + (
            f"\n  - and {more} more" if more > 0 else "")

    if launch.preexisting(work, base, must_fix=baseline.must_fix):
        baseline.found = {"command": command, "base": baseline.base, "n": len(work), "base_n": len(base),
                          "fixed": len(base - work), "added": 0,
                          "failures": [clip(x, 200, one_line=True) for x in sorted(work)[:FAILURES_SHOWN]]}
        with baseline._db() as db:
            db.add_event(baseline.task_id, "check_preexisting", **baseline.found)
        return True, ""
    if not new:
        # Only work that must make a failure go away gets here: the same failures stand.
        return False, (f"this task's job is to make a failure go away, and the same {len(work)} failure(s) fail "
                       f"at the base commit {short} as here: none of them is gone yet\n{listed(old)}")
    said = f"compared with the same check at the base commit {short}: {len(new)} failing line(s) are new with this " \
           f"work, focus on them:\n{listed(new)}"
    if old:
        said += f"\n{len(old)} already failed at the base, before this work (not this task's to fix):\n{listed(old)}"
    return False, said


def preexisting_line(found: Mapping[str, Any] | None) -> str:
    """The report's line for a check whose failures were all there before the work."""
    if not found:
        return ""
    fixed, n, command = int(found.get("fixed") or 0), int(found.get("n") or 0), found.get("command")
    failures = f"{n} failure{'' if n == 1 else 's'}"
    if not fixed:
        return (f"`{command}` fails at the base too, with the same {failures}: they were there before this work, "
                "not caused by it")
    return (f"`{command}` fails at the base too: its {failures} here were all there before this work, not caused "
            f"by it, and {fixed} more that failed at the base pass now")


#: What a check prints when it could not run at all — its script, its manifest
#: or the Node its tools need is missing. Its exit then says nothing about the
#: work, and a stronger model meets the same wall. npm's EBADENGINE *warning*
#: is not here: it prints on runs that go on and pass.
_CANNOT_RUN = re.compile(
    r"^npm (?:ERR!|error) Missing script: .*$"
    r"|^npm (?:ERR!|error) enoent Could not read package\.json.*$"
    r"|^.*\brequires (?:a minimum )?Node\.js version\b.*$"
    r"|^.*The engine \"node\" is incompatible with this module.*$",
    re.MULTILINE)


def cannot_run(code: int | None, output: str) -> str | None:
    """Why a check that exited non-zero could not run at all, or None when it ran
    and failed. Exit 127 is the shell's "command not found"."""
    if code is None or code == 0:
        return None
    found = _CANNOT_RUN.search(output)
    if found:
        return found.group(0).strip()[:300]
    if code == 127:
        return "exit 127: the shell did not find the command"
    return None


#: A red check with at most this many failing tests runs once more: one that passes then is flaky.
FLAKY_MAX = 3
#: Where a repository's flaky tests are remembered (`Store.meta`), each with how often it flaked.
FLAKY_META = "flaky:"


#: The most of a repository's known flaky tests a brief names.
FLAKY_BRIEFED = 5


def flaky_key(repo_dir: Path) -> str:
    """Where a repository's flaky tests are kept: by its key, so every worktree and session
    of it reads the same ones."""
    return FLAKY_META + hashlib.sha256(str(repo.key(repo_dir) or repo_dir).encode()).hexdigest()[:24]


def known_flaky(store: Store, repo_dir: Path) -> list[str]:
    """The tests cauce saw flake here (failed, then passed on the rerun), most often first."""
    try:
        seen = json.loads(store.meta(flaky_key(repo_dir)) or "{}")
    except (ValueError, sqlite3.Error):
        return []
    return [line for line, _ in sorted(seen.items(), key=lambda kv: -int(kv[1] or 0))][:FLAKY_BRIEFED]


def _steady_check(command: str, workdir: Path, baseline: Baseline | None) -> tuple[int | None, str]:
    """The check, run again once when it ran and failed on a few tests (`FLAKY_MAX`): a second
    run that passes means those tests flake, which says nothing of the work. Its pass stands,
    recorded as a `check_flaky` event with the tests and kept per repository; anything else is
    the first run's failure, unchanged."""
    code, full = _run_check(command, workdir)
    if not _ran_and_failed(code, full):
        return code, full
    failing = launch.check_failures(full, roots=(workdir,))
    if not failing or len(failing) > FLAKY_MAX:
        return code, full
    again, second = _run_check(command, workdir)
    if again != 0:
        return code, full
    lines = sorted(clip(x, 200, one_line=True) for x in failing)
    if baseline is not None:
        with baseline._db() as db:
            db.add_event(baseline.task_id, "check_flaky", command=command, failures=lines)
            key = flaky_key(baseline.repo_dir)
            seen = json.loads(db.meta(key) or "{}")
            for line in lines:
                seen[line] = int(seen.get(line) or 0) + 1
            db.set_meta(key, json.dumps(dict(sorted(seen.items(), key=lambda kv: -kv[1])[:50])))
    return 0, second + "\n\n" + f"(cauce: {len(lines)} test(s) failed on the first run and passed on the second: " \
        + "; ".join(lines) + ")"


def _verified(result: launch.WorkerResult, command: str, workdir: Path,
              baseline: Baseline | None = None) -> launch.WorkerResult:
    """A claimed pass checked by the repository's own command. The worker does not
    grade itself: a red check turns the pass into a code bug, with the check's
    output as the evidence the next attempt reads. A check that could not run at
    all is the environment's, never a pass and never a bug in the work. A red check
    whose every failing line also fails at the base (`baseline`) was there before the
    work: the pass stands, and that is recorded and said (`_against_base`)."""
    if baseline is not None:
        baseline.found = None
    code, full = _steady_check(command, workdir, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if code is None:
        return dataclasses.replace(result, passed=False, failure=Failure.INCONCLUSIVE,
                                   summary=f"`{command}` timed out after the claimed pass")
    if code == 0:
        return result
    if (why := cannot_run(code, output)) is not None:
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output,
            summary=f"claimed a pass, but `{command}` could not run here ({why}): {result.summary}",
        )
    if timed := launch.check_timed_out(output):
        # The worker's own run passed and most of what failed here are tests out of time:
        # a machine busier than its CPUs, not the work. The level halves on it (`starved`).
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output, load=timed, starved="load",
            summary=f"claimed a pass, but `{command}` timed its tests out, as a loaded machine does ({timed}): "
                    f"{result.summary}",
        )
    if dropped := launch.check_offline(output):
        # The check could not reach the network (an install, a fetch): its exit says
        # nothing about the work, and the same check, waited for, may well pass.
        return dataclasses.replace(
            result, passed=False, failure=Failure.ENVIRONMENT, evidence=output, network=dropped,
            summary=f"claimed a pass, but `{command}` lost the network ({dropped}): {result.summary}",
        )
    before, compared = _against_base(baseline, command, full, workdir)
    if before:
        return result
    return dataclasses.replace(
        result, passed=False, failure=Failure.CODE_BUG,
        evidence=f"{output}\n\n{compared}" if compared else output,
        summary=f"claimed a pass, but `{command}` exited {code}: {result.summary}",
    )


#: Attempts that stopped before the worker could check its own work, through no
#: verdict of its own: the repository's check is run for them.
_UNCHECKED = frozenset({Failure.TURNS_EXHAUSTED})


def _settles(result: launch.WorkerResult, command: str | None) -> bool:
    """A worker that could not run the command that would show its work — refused
    it, or ran out of turns first — and did not call it a failure: on a
    task that has written something, the repository's own check can still
    decide. Its claim alone never would. Run by cauce, the check costs the
    worker nothing, and a red one hands the next attempt the failing output."""
    if not command or result.passed:
        return False
    if result.failure is Failure.PERMISSION:
        return result.verdict in ("inconclusive", "pass")
    return result.failure in _UNCHECKED


def _ran_and_failed(code: int | None, output: str) -> bool:
    """A check that ran and failed on the work: not a timeout, not one that could not run,
    not a loaded machine's or a dropped network's. Only such a failure is compared with the base."""
    return bool(code) and cannot_run(code, output) is None and not launch.check_timed_out(output) \
        and not launch.check_offline(output)


def _settled(result: launch.WorkerResult, command: str, workdir: Path,
             baseline: Baseline | None = None) -> launch.WorkerResult:
    if baseline is not None:
        baseline.found = None
    code, full = _steady_check(command, workdir, baseline)
    output = full[-CHECK_OUTPUT_KEPT:]
    if result.failure is Failure.PERMISSION:
        cause = f"the worker was refused {', '.join(result.denied[:3])}"
    else:
        cause = "the worker ran out of turns before checking its work"
    before, compared = _against_base(baseline, command, full, workdir) if _ran_and_failed(code, full) \
        else (False, "")
    if before:
        return dataclasses.replace(
            result, passed=True, failure=None, evidence=output,
            summary=f"{result.summary} ({cause}; `{command}` failed only as it does at the base, in its place)")
    if code != 0:
        why = "timed out" if code is None else f"exited {code}"
        evidence = f"`{command}` {why}:\n{output}".strip() + (f"\n\n{compared}" if compared else "")
        return dataclasses.replace(result, evidence=evidence,
                                   summary=f"{result.summary} ({cause}; cauce ran `{command}`, which {why})")
    return dataclasses.replace(
        result, passed=True, failure=None, evidence=output or f"`{command}` exited 0",
        summary=f"{result.summary} ({cause}; `{command}` passed in its place)",
    )


def _only_the_base(result: launch.WorkerResult, command: str | None, baseline: Baseline | None) -> bool:
    """A worker that stopped on a point (`spec_bug`, `architecture_bug`) whose point is the
    check failing on what already failed at the base: the check's failures were all there
    before the work (`baseline.found`), the worker names the check or one of those failures,
    and every criterion it answered is met. Anything else stays a decision."""
    if not command or baseline is None or not baseline.found:
        return False
    if any(not c.get("met") for c in result.criteria):
        return False
    said = f"{result.summary}\n{result.evidence}"
    failing = [str(line) for line in baseline.found.get("failures") or ()]
    return command in said or any(_named(line, said) for line in failing)


def _named(line: str, said: str) -> bool:
    """A failing line a worker's words name: its longest path or test name appears in them."""
    words = sorted((w for w in re.split(r"[\s:'\"()]+", line) if len(w) >= 6), key=len, reverse=True)
    return bool(words) and words[0] in said


def _stands(result: launch.WorkerResult, command: str | None, workdir: Path,
            baseline: Baseline | None = None) -> str:
    """Whether the work of a worker that reported a failure of the task (`spec_bug`,
    `architecture_bug`) stands anyway: "cauce" when the repository's check, run by cauce,
    passes on it; "worker" when there is none to run and the worker's own check is its
    evidence (shown, with at most the one criterion the open point leaves unmet); "" otherwise."""
    if command:
        if baseline is not None:
            baseline.found = None
        code, output = _steady_check(command, workdir, baseline)
        return "cauce" if code == 0 or (_ran_and_failed(code, output) and _against_base(
            baseline, command, output, workdir)[0]) else ""
    unmet = sum(1 for c in result.criteria if not c.get("met"))
    return "worker" if result.evidence.strip() and unmet <= 1 and unmet < max(len(result.criteria), 1) else ""


def _note(store: Store, task: dict, key: str | None, report: Report, the_plan: Plan, checkout: Path,
          commit: str | None, ask: notes.Ask | None) -> None:
    """File what the passing worker learned in the project's notes, anchored to
    the files it changed as its branch has them. Never fails the run: the work
    passed, and a note that could not be filed is said in the report."""
    if not key:
        return
    lookups = [(a, s) for a, s in the_plan.active if hasattr(a, "symbols_named")]

    def symbols_of(fact: str) -> list[tuple[str, str]]:
        return [x for adapter, status in lookups for x in adapter.symbols_named(fact, status)]

    try:
        report.noted = notes.file_facts(
            store, key, report.learned, author="worker", source=f"task #{task['id']}", task_id=task["id"],
            repo_dir=checkout, paths=report.changed, commit=commit, symbols_of=symbols_of if lookups else None,
            ask=ask)
    except Exception as exc:  # a pass is not undone by its notes
        report.impact.append(f"notes: what the worker learned could not be filed ({type(exc).__name__}: {exc})")
        return
    if report.noted:
        store.add_event(task["id"], "noted", notes=report.noted)


def _to_engram(store: Store, task: dict, report: Report, the_plan: Plan, checkout: Path) -> None:
    """Give the team's engram what a passing worker learned, when the person turned
    it on. The facts are the ones notes would keep: never a secret. Never fails
    the run, and a save that failed is said in the report."""
    found = next(((a, s) for a, s in the_plan.active if a.name == "engram"), None)
    if found is None:
        report.impact.append("engram: what the worker learned was not saved (no verified engram server answered)")
        return
    adapter, status = found
    facts = [f for f in (notes.clean_fact(x) for x in report.learned) if f]
    try:
        saved = adapter.remember(facts, project=str(status.data.get("project") or ""), task_id=task["id"],
                                 title=task["title"], directory=checkout)
    except Exception as exc:  # a pass is not undone by a neighbour
        report.impact.append(f"engram: what the worker learned could not be saved ({type(exc).__name__}: {exc})")
        return
    if saved:
        report.impact.append(f"engram: saved {len(saved)} memories to {status.data.get('project')} "
                             f"(#{', #'.join(map(str, saved))})")
        store.add_event(task["id"], "engram_saved", project=status.data.get("project"), ids=saved)


def _remember(store: Store, task: dict, key: str | None, attempt: Attempt, outcome: str,
              result: launch.WorkerResult, *, role: str | None, kind: str) -> None:
    """A fix kept with its cell: the role the worker wore and the areas of what it changed
    (of its request when it changed nothing), matched later by the work's own text."""
    goal = cells.memory_text(store, task)
    problem = store.open_problem(task["title"], repo=key, symptom=goal[:1000])
    store.add_fix(
        problem, f"{attempt.cell.label}: {result.summary}", outcome, repo=key,
        why=str(attempt.failure or "") if outcome == "failed" else "",
        evidence=result.evidence, task_id=task["id"], role=role,
        areas=cells.of_paths(result.changed_paths) or cells.of_request(goal, kind),
    )
