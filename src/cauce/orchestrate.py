"""The loop: classify, choose a starting cell, attempt, check, move, remember.

Everything here is deterministic code. The models are called for two things
only — to classify a request the rules did not recognise, and to do the work —
and nothing a model says about its own work decides the next move by itself:
the result block is checked for evidence, an optional verify command has the
last word on a claimed pass, and the failure kind is read against the ladder by
`escalate.decide`.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cauce import capabilities as caps
from cauce import config, isolate, launch, repo
from cauce.adapters import ABSENT, Adapter, Status, default_adapters
from cauce.classify import Classification, classify
from cauce.escalate import Attempt, Decision, Failure, Move, decide
from cauce.matrix import READ_ONLY_KINDS, Cell, ladder_for
from cauce.store import Store, home

#: Failures that describe something tried against the problem, worth keeping
#: as a dead end. A timeout or a turn ceiling says nothing about the fix.
_MEMORABLE = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.APPROACH, Failure.SPEC_BUG,
                        Failure.ARCHITECTURE_BUG})

class Cancelled(Exception):
    """Raised into a run when someone cancels it; the run stops and cleans up."""


_FINAL_STATUS = {
    Move.REPLAN: "replan",
    Move.NEEDS_APPROVAL: "needs_approval",
    Move.EXHAUSTED: "failed",
    Move.BLOCKED: "blocked",
}

#: How many finished tasks of a kind it takes before history moves the start.
MIN_REPO_SAMPLES = 3
MIN_GLOBAL_SAMPLES = 5
VERIFY_TIMEOUT_S = 900

Launcher = Callable[[launch.LaunchSpec], launch.WorkerResult]


@dataclass
class Options:
    budget_usd: float = 5.0
    max_attempts: int = 6
    allow_approval: bool = False
    isolate: bool = True
    verify: str | None = None
    launch_dir: Path | None = None
    max_turns: int = launch.DEFAULT_MAX_TURNS
    use_model_classifier: bool = True
    kind: str | None = None
    start: Cell | None = None
    dry_run: bool = False
    #: None reads the `livespec` setting (on by default).
    livespec: bool | None = None


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


@dataclass
class Report:
    task_id: int | None
    status: str
    plan: Plan
    cells: list[str] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    final_cell: str | None = None
    cost_usd: float = 0.0
    branch: str | None = None
    summary: str = ""
    impact: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            f"task #{self.task_id}: {self.status}" if self.task_id else f"plan: {self.status}",
            f"kind {self.plan.kind} ({self.plan.classification.source}: {self.plan.classification.reason})",
            f"ladder {' → '.join(c.label for c in self.plan.ladder)}",
            f"start {self.plan.start.label}" + "".join(f"\n  · {r}" for r in self.plan.reasons),
        ]
        lines += [status.line() for status in self.plan.neighbours]
        if self.task_id is None:
            # A dry run shows what the first worker would be handed.
            lines += self.plan.context
        if self.plan.capabilities:
            lines.append(f"capabilities {', '.join(self.plan.capabilities)}")
        for i, (cell, move) in enumerate(zip(self.cells, [*self.moves, ""], strict=False), 1):
            lines.append(f"attempt {i} {cell}" + (f" → {move}" if move else ""))
        if self.final_cell:
            lines.append(f"passed at {self.final_cell}")
        if self.cost_usd:
            lines.append(f"cost ${self.cost_usd:.2f}")
        if self.branch:
            lines.append(f"branch {self.branch} — review it, then merge it")
        lines += self.impact
        if self.summary:
            lines.append(self.summary)
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
    if not config.enabled("livespec", os.environ, options.livespec):
        return []
    return default_adapters()


def plan(text: str, repo_dir: Path, store: Store, options: Options, registry: Mapping[str, caps.Capability],
         *, classifier: Callable[[str], Classification] | None = None,
         adapters: Sequence[Adapter] | None = None) -> Plan:
    if options.kind:
        classification = Classification(options.kind, "medium", "rule", "chosen by the caller")
    elif classifier is not None:
        classification = classifier(text)
    else:
        classification = classify(text, use_model=options.use_model_classifier, cwd=home())
    kind = classification.kind

    neighbour_notes: list[str] = []
    statuses: list[Status] = []
    active: list[tuple[Adapter, Status]] = []
    context: list[str] = []
    raise_rungs, critical = 0, False
    for adapter in adopted(options, adapters):
        status = adapter.inspect(repo_dir)
        runnable_absent = status.state == ABSENT and adapter.server() is not None
        if runnable_absent or status.stale:
            if options.dry_run:
                neighbour_notes.append(f"{adapter.name}: would {'index' if runnable_absent else 'refresh'} first")
            else:
                neighbour_notes.append(f"{adapter.name}: {adapter.refresh(repo_dir)}")
                status = adapter.inspect(repo_dir)
        statuses.append(status)
        if not status.present:
            continue
        active.append((adapter, status))
        briefing = adapter.brief(text, repo_dir, status)
        context += briefing.lines
        raise_rungs = max(raise_rungs, briefing.raise_rungs)
        critical = critical or briefing.critical
        neighbour_notes += briefing.reasons

    if critical and kind == "review-routine":
        kind = "review-critical"
        neighbour_notes.append("the code under review implements a critical spec: reviewed as critical")
    ladder = ladder_for(kind)
    if options.start is not None:
        start, reasons = options.start, ["pinned by the caller"]
    else:
        key = repo.key(repo_dir)
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
    selected = caps.select(registry, kind, workdir=repo_dir)
    names = tuple(dict.fromkeys([*selected.names, *(a.name for a, _ in active)]))
    return Plan(kind, classification, ladder, start, reasons + neighbour_notes, names,
                tuple(context), tuple(statuses), tuple(active))


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
) -> Report:
    options = options or Options()
    registry = registry if registry is not None else caps.load(home() / "capabilities.json")
    launcher = launcher or launch.run
    repo_dir = repo_dir.resolve()
    the_plan = plan(text, repo_dir, store, options, registry, classifier=classifier, adapters=adapters)
    if options.dry_run:
        return Report(None, "dry run", the_plan)

    key = repo.key(repo_dir)
    c = the_plan.classification
    fields = dict(
        repo=key, cwd=str(repo_dir), kind=the_plan.kind, complexity=c.complexity, class_source=c.source,
        class_reason=c.reason, start_cell=the_plan.start.label, pinned=int(options.start is not None),
    )
    if task_id is not None:
        # A queued task the dispatcher claimed: it keeps its id, its messages
        # and its place in the lane's history.
        store.update_task(task_id, status="running", source="cauce", cost_usd=c.cost_usd, **fields)
        task = store.get_task(task_id)
    else:
        task = store.create_task(text, status="running", source="cauce", cost_usd=c.cost_usd, **fields)
    report = Report(task["id"], "running", the_plan, cost_usd=c.cost_usd)
    store.update_task(task["id"], pid=os.getpid())
    store.add_event(task["id"], "planned", kind=the_plan.kind, start=the_plan.start.label,
                    ladder=[x.label for x in the_plan.ladder], reasons=the_plan.reasons,
                    neighbours=[st.line() for st in the_plan.neighbours])

    workspace = None
    writes = the_plan.kind not in READ_ONLY_KINDS
    if options.isolate and writes and repo.toplevel(repo_dir) is not None:
        workspace = isolate.prepare(repo_dir, task["id"], home())
    workdir = workspace.path if workspace else repo_dir
    launch_dir = options.launch_dir.resolve() if options.launch_dir else workdir

    dead = store.dead_ends(text, repo=key, limit=5)
    cell, turns = the_plan.start, options.max_turns
    attempts: list[Attempt] = []
    decision: Decision | None = None
    try:
        while True:
            remaining = options.budget_usd - report.cost_usd
            if remaining < 0.01:
                report.status, report.summary = "blocked", f"the ${options.budget_usd:.2f} budget is spent"
                break
            if len(attempts) >= options.max_attempts:
                report.status, report.summary = "failed", f"{options.max_attempts} attempts without a pass"
                break
            if store.cancel_requested(task["id"]):
                raise Cancelled
            selection = _equip(caps.select(registry, the_plan.kind, failed_before=bool(attempts), workdir=workdir),
                               the_plan, repo_dir, workdir)
            spec = launch.LaunchSpec(
                prompt=brief(text, the_plan.kind, workdir, attempts, dead, options.verify, writes,
                             the_plan.context),
                cell=cell,
                launch_dir=launch_dir,
                workdir=workdir if workdir != launch_dir else None,
                max_turns=turns,
                max_budget_usd=round(remaining, 2),
                disallowed_tools=() if writes else launch.WRITE_TOOLS,
                mcp_servers=selection.servers,
                append_system_prompt=selection.system_prompt(),
            )
            store.update_task(task["id"], current_cell=cell.label)
            store.add_event(task["id"], "attempt_started", seq=len(attempts) + 1, cell=cell.label,
                            max_turns=turns, budget_usd=spec.max_budget_usd, capabilities=list(selection.names))
            result = launcher(spec)
            if result.passed and options.verify:
                result = _verified(result, options.verify, workdir)
            report.cells.append(cell.label)
            report.cost_usd += result.cost_usd
            seq = store.add_attempt(
                task["id"], cell=cell.label, max_turns=turns, passed=int(result.passed),
                failure=result.failure.value if result.failure else None, summary=result.summary,
                evidence=result.evidence[:4000], cost_usd=result.cost_usd,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens, turns=result.turns,
                duration_s=result.duration_s, changed_paths=result.changed_paths,
                capabilities=selection.names,
            )
            store.add_event(task["id"], "attempt_finished", seq=seq, cell=cell.label, passed=result.passed,
                            failure=result.failure.value if result.failure else None, cost_usd=result.cost_usd,
                            turns=result.turns, summary=result.summary[:500])
            attempt = Attempt(cell, turns, result.passed, result.failure, result.summary)
            attempts.append(attempt)
            if result.passed:
                report.status, report.final_cell, report.summary = "done", cell.label, result.summary
                for adapter, status in the_plan.active:
                    report.impact += adapter.assess(result.changed_paths, repo_dir, status)
                if len(attempts) > 1:
                    _remember(store, task, key, attempt, "worked", result)
                break
            if result.failure in _MEMORABLE and result.summary:
                _remember(store, task, key, attempt, "failed", result)
            decision = decide(the_plan.ladder, attempts, allow_approval=options.allow_approval)
            store.set_move(task["id"], seq, decision.move.value, decision.reason)
            report.moves.append(f"{decision.move.value}: {decision.reason}")
            store.add_event(task["id"], "moved", move=decision.move.value, reason=decision.reason,
                            next_cell=decision.cell.label if decision.cell else None)
            if not decision.continues:
                # The worker's own account goes with the reason: "the task is
                # wrong" is only actionable next to what it found wrong.
                report.status = _FINAL_STATUS[decision.move]
                report.summary = f"{decision.reason}. Last attempt: {result.summary}" if result.summary else (
                    decision.reason)
                break
            if decision.move is Move.NEXT_MODEL and workspace is not None:
                isolate.reset(workspace)
            cell, turns = decision.cell, decision.max_turns
    except (Cancelled, KeyboardInterrupt):
        report.status, report.summary = "cancelled", "cancelled before it finished"
    finally:
        if workspace is not None:
            report.branch = isolate.finish(
                workspace, keep=report.status == "done", message=f"cauce task #{task['id']}: {task['title']}"
            )
        status = report.status if report.status != "running" else "failed"
        store.update_task(task["id"], status=status, final_cell=report.final_cell, result=report.summary,
                          pid=None, current_cell=None)
        if status != "done" and store.get_task(task["id"])["dispatched"]:
            store.pause_lane(key, f"task #{task['id']} ended {status}: {report.summary[:200]}")
        store.add_event(task["id"], "finished", status=status, final_cell=report.final_cell,
                        cost_usd=round(report.cost_usd, 4), branch=report.branch, impact=report.impact)
        store.add_message(task["id"], "worker", report.text())
    return report


def brief(
    text: str,
    kind: str,
    workdir: Path,
    attempts: Sequence[Attempt],
    dead_ends: Sequence[dict],
    verify: str | None,
    writes: bool,
    context: Sequence[str] = (),
) -> str:
    parts = [f"Task ({kind}):\n{text}", f"Work in: {workdir}"]
    if context:
        parts.append("\n".join(context))
    if not writes:
        parts.append("This task is read-only: find out and report. Do not change files.")
    if verify:
        parts.append(f"When you report a pass, it is checked by running: {verify}")
    if dead_ends:
        lines = ["Fixes already tried against similar problems, here or in another repository. "
                 "Do not repeat one without saying why it would work this time:"]
        for d in dead_ends:
            line = f"- ({d['repo']}) {d['problem']}: tried {d['tried'][:200]}"
            if d["why"]:
                line += f"; failed because {d['why'][:200]}"
            if d["worked_instead"]:
                line += f"; what worked: {d['worked_instead'][:200]}"
            lines.append(line)
        parts.append("\n".join(lines))
    if attempts:
        lines = ["Previous attempts on this task — already tried, do not repeat:"]
        for i, a in enumerate(attempts, 1):
            lines.append(f"  attempt {i} [{a.cell.label}, {a.failure or 'no verdict'}] {a.summary[:300]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


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
    return caps.Selection(servers, tuple(hints), tuple(names))


def _verified(result: launch.WorkerResult, command: str, workdir: Path) -> launch.WorkerResult:
    """A claimed pass checked by the repository's own command. The worker does not
    grade itself: a red check turns the pass into a code bug, with the check's
    output as the evidence the next attempt reads."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir), capture_output=True, text=True,  # noqa: S602
                              timeout=VERIFY_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return launch.WorkerResult(False, Failure.INCONCLUSIVE, f"`{command}` timed out after the claimed pass",
                                   result.evidence, result.changed_paths, result.cost_usd, result.input_tokens,
                                   result.output_tokens, result.turns, result.duration_s, result.raw)
    if proc.returncode == 0:
        return result
    output = (proc.stdout + proc.stderr).strip()[-3000:]
    return launch.WorkerResult(
        False, Failure.CODE_BUG, f"claimed a pass, but `{command}` exited {proc.returncode}: {result.summary}",
        output, result.changed_paths, result.cost_usd, result.input_tokens, result.output_tokens,
        result.turns, result.duration_s, result.raw,
    )


def _remember(store: Store, task: dict, key: str | None, attempt: Attempt, outcome: str,
              result: launch.WorkerResult) -> None:
    problem = store.open_problem(task["title"], repo=key, symptom=task["body"][:1000])
    store.add_fix(
        problem, f"{attempt.cell.label}: {result.summary}", outcome, repo=key,
        why=str(attempt.failure or "") if outcome == "failed" else "",
        evidence=result.evidence, task_id=task["id"],
    )
