"""The loop: classify, choose a starting cell, attempt, check, move, remember.

Everything here is deterministic code. The models are called for two things
only — to classify a request the rules did not recognise, and to do the work —
and nothing a model says about its own work decides the next move by itself:
the result block is checked for evidence, an optional verify command has the
last word on a claimed pass, and the failure kind is read against the ladder by
`escalate.decide`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cauce import capabilities as caps
from cauce import isolate, launch, repo
from cauce.classify import Classification, classify
from cauce.escalate import Attempt, Decision, Failure, Move, decide
from cauce.matrix import READ_ONLY_KINDS, Cell, ladder_for
from cauce.store import Store, home

#: Failures that describe something tried against the problem, worth keeping
#: as a dead end. A timeout or a turn ceiling says nothing about the fix.
_MEMORABLE = frozenset({Failure.CODE_BUG, Failure.TEST_BUG, Failure.APPROACH, Failure.SPEC_BUG,
                        Failure.ARCHITECTURE_BUG})

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


@dataclass
class Plan:
    kind: str
    classification: Classification
    ladder: tuple[Cell, ...]
    start: Cell
    reasons: list[str]
    capabilities: tuple[str, ...]


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

    def text(self) -> str:
        lines = [
            f"task #{self.task_id}: {self.status}" if self.task_id else f"plan: {self.status}",
            f"kind {self.plan.kind} ({self.plan.classification.source}: {self.plan.classification.reason})",
            f"ladder {' → '.join(c.label for c in self.plan.ladder)}",
            f"start {self.plan.start.label}" + "".join(f"\n  · {r}" for r in self.plan.reasons),
        ]
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


def plan(text: str, repo_dir: Path, store: Store, options: Options, registry: Mapping[str, caps.Capability],
         *, classifier: Callable[[str], Classification] | None = None) -> Plan:
    if options.kind:
        classification = Classification(options.kind, "medium", "rule", "chosen by the caller")
    elif classifier is not None:
        classification = classifier(text)
    else:
        classification = classify(text, use_model=options.use_model_classifier, cwd=home())
    kind = classification.kind
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
    selected = caps.select(registry, kind, workdir=repo_dir)
    return Plan(kind, classification, ladder, start, reasons, selected.names)


def run(
    text: str,
    repo_dir: Path,
    store: Store,
    options: Options | None = None,
    *,
    registry: Mapping[str, caps.Capability] | None = None,
    launcher: Launcher | None = None,
    classifier: Callable[[str], Classification] | None = None,
) -> Report:
    options = options or Options()
    registry = registry if registry is not None else caps.load(home() / "capabilities.json")
    launcher = launcher or launch.run
    repo_dir = repo_dir.resolve()
    the_plan = plan(text, repo_dir, store, options, registry, classifier=classifier)
    if options.dry_run:
        return Report(None, "dry run", the_plan)

    key = repo.key(repo_dir)
    c = the_plan.classification
    task = store.create_task(
        text, status="running", source="cauce", repo=key, cwd=str(repo_dir), kind=the_plan.kind,
        complexity=c.complexity, class_source=c.source, class_reason=c.reason,
        start_cell=the_plan.start.label, cost_usd=c.cost_usd,
    )
    report = Report(task["id"], "running", the_plan, cost_usd=c.cost_usd)

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
            selection = caps.select(registry, the_plan.kind, failed_before=bool(attempts), workdir=workdir)
            spec = launch.LaunchSpec(
                prompt=brief(text, the_plan.kind, workdir, attempts, dead, options.verify, writes),
                cell=cell,
                launch_dir=launch_dir,
                workdir=workdir if workdir != launch_dir else None,
                max_turns=turns,
                max_budget_usd=round(remaining, 2),
                disallowed_tools=() if writes else launch.WRITE_TOOLS,
                mcp_servers=selection.servers,
                append_system_prompt=selection.system_prompt(),
            )
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
            attempt = Attempt(cell, turns, result.passed, result.failure, result.summary)
            attempts.append(attempt)
            if result.passed:
                report.status, report.final_cell, report.summary = "done", cell.label, result.summary
                if len(attempts) > 1:
                    _remember(store, task, key, attempt, "worked", result)
                break
            if result.failure in _MEMORABLE and result.summary:
                _remember(store, task, key, attempt, "failed", result)
            decision = decide(the_plan.ladder, attempts, allow_approval=options.allow_approval)
            store.set_move(task["id"], seq, decision.move.value, decision.reason)
            report.moves.append(f"{decision.move.value}: {decision.reason}")
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
    finally:
        if workspace is not None:
            report.branch = isolate.finish(
                workspace, keep=report.status == "done", message=f"cauce task #{task['id']}: {task['title']}"
            )
        store.update_task(
            task["id"], status=report.status if report.status != "running" else "failed",
            final_cell=report.final_cell, result=report.summary,
        )
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
) -> str:
    parts = [f"Task ({kind}):\n{text}", f"Work in: {workdir}"]
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
