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
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cauce import allow as allow_rules
from cauce import capabilities as caps
from cauce import config, habits, isolate, launch, models, project, repo, stops
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

#: Endings that wait on a person, not on the task being rewritten: the work done
#: so far is kept on the task's branch, unverified, for them to see and resume.
KEEPS_WORK = frozenset({"done", "blocked", "needs_approval"})

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
    #: Permission rules a person granted the workers, e.g. `Bash(npm run build)`.
    allow_tools: tuple[str, ...] = ()

    def stored(self) -> dict:
        """What a resumed run is started with again, as the task's `options`. A run
        in the checkout itself resumes there: in a worktree it could not see the
        uncommitted work it was continuing."""
        values = {"budget_usd": self.budget_usd, "verify": self.verify, "kind": self.kind,
                  "allow_approval": self.allow_approval, "allow_tools": list(self.allow_tools),
                  "livespec": self.livespec, "no_isolate": not self.isolate}
        return {k: v for k, v in values.items() if v not in (None, [], False)}


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
    #: Whether it worked in a worktree; outside one, its changes are in the checkout.
    isolated: bool = False
    #: Why it stopped short of a pass, and who made that call.
    stop: stops.Stop | None = None

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
        if self.branch and self.status == "done":
            lines.append(f"branch {self.branch} — review it, then merge it; your checkout has none of it until then")
        elif self.branch:
            lines.append(f"branch {self.branch} keeps this task's unverified work: review it with "
                         f"`git diff HEAD...{self.branch}`, or `cauce resume {self.task_id}` once the block is cleared")
        elif self.task_id and self.status != "done" and self.changed:
            lines.append("nothing of it was kept" if self.isolated else
                         "these changes are in your checkout, unverified")
        lines += self.impact
        if self.summary:
            lines.append(self.summary)
        if self.stop is not None and self.task_id:
            lines.append(f"stopped by {stops.WHO.get(self.stop.who, self.stop.who)}: {stops.what_to_do(self.stop)}")
            command = stops.next_step(self.task_id, self.stop)
            if command:
                lines.append(f"  {command}")
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
    habits.load_events(store)
    learned = habits.recipes(store, kind, repo=repo.key(repo_dir))
    if learned:
        context.append("Steps that came before passing attempts on this kind of task here: "
                       + "; ".join(learned) + ". Use them unless the task says otherwise.")
    neighbour_notes += _unmet(text, registry)
    for lag in models.lagging(store):
        neighbour_notes.append(f"workers last ran {lag['workers']} for `{lag['alias']}` while your sessions use "
                               f"{lag['sessions']}: the installed Claude Code's alias is older. Pin it with "
                               f"`{lag['command']}`, or update Claude Code")
    seen = _refused_here(store, repo.key(repo_dir), options.allow_tools)
    if seen:
        neighbour_notes.append("workers here were refused before: " + ", ".join(f"{r} ×{n}" for r, n in seen)
                               + ". Pass the ones this task needs with --allow, or a worker stops on them")
    expected = store.expected_cost(kind, repo=repo.key(repo_dir)) or store.expected_cost(kind)
    if expected:
        neighbour_notes.append(f"finished {kind} tasks cost ${expected[0]:.2f} on average (last {expected[1]})")
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
    session_id: str | None = None,
    history: Sequence[Attempt] = (),
) -> Report:
    """Run a task. `history` is a resumed task's earlier attempts: shown to its
    workers, never counted by the escalation, which starts over."""
    options = options or Options()
    registry = registry if registry is not None else caps.load(home() / "capabilities.json")
    launcher = launcher or launch.run
    repo_dir = repo_dir.resolve()
    the_plan = plan(text, repo_dir, store, options, registry, classifier=classifier, adapters=adapters)
    if options.dry_run:
        return Report(None, "dry run", the_plan)
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(repo_dir)  # work run here: the project is cauce's

    key = repo.key(repo_dir)
    c = the_plan.classification
    fields = dict(
        repo=key, cwd=str(repo_dir), kind=the_plan.kind, complexity=c.complexity, class_source=c.source,
        class_reason=c.reason, start_cell=the_plan.start.label, pinned=int(options.start is not None),
    )
    if task_id is not None:
        # A queued or resumed task: it keeps its id, its messages, its cost so
        # far and its place in the lane's history.
        before = store.get_task(task_id)
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
    store.add_event(task["id"], "planned", kind=the_plan.kind, start=the_plan.start.label,
                    ladder=[x.label for x in the_plan.ladder], reasons=the_plan.reasons,
                    neighbours=[st.line() for st in the_plan.neighbours])

    workspace = None
    writes = the_plan.kind not in READ_ONLY_KINDS
    context = list(the_plan.context)
    if options.isolate and writes and repo.toplevel(repo_dir) is not None:
        resumed = isolate.kept(repo_dir, task["id"])
        workspace = isolate.prepare(repo_dir, task["id"], home())
        if resumed:
            context.append("This task was stopped before and is resumed: what its earlier attempts wrote is "
                           "already here. Check it, and finish it rather than starting over.")
        if workspace.links:
            context.append("Linked from the person's checkout and shared with it: "
                           + ", ".join(workspace.links) + ". Use them; do not delete or reinstall them.")
    workdir = workspace.path if workspace else repo_dir
    launch_dir = options.launch_dir.resolve() if options.launch_dir else workdir

    dead = store.dead_ends(text, repo=key, limit=5)
    cell, turns = the_plan.start, options.max_turns
    attempts: list[Attempt] = []
    changed: set[str] = set()
    decision: Decision | None = None
    try:
        while True:
            remaining = options.budget_usd - report.cost_usd
            if remaining < 0.01:
                report.stop = stops.Stop("budget", f"the ${options.budget_usd:.2f} budget is spent",
                                         budget_usd=options.budget_usd, attempt=len(attempts) or None)
                report.status, report.summary = "blocked", report.stop.reason
                break
            if len(attempts) >= options.max_attempts:
                report.stop = stops.Stop("attempts", f"{options.max_attempts} attempts without a pass",
                                         attempt=len(attempts))
                report.status, report.summary = "failed", report.stop.reason
                break
            if store.cancel_requested(task["id"]):
                raise Cancelled
            selection = _equip(caps.select(registry, the_plan.kind, failed_before=bool(attempts), workdir=workdir),
                               the_plan, repo_dir, workdir)
            spec = launch.LaunchSpec(
                model_id=models.pinned(cell.model),
                prompt=brief(text, the_plan.kind, workdir, [*history, *attempts], dead, options.verify, writes,
                             context),
                cell=cell,
                launch_dir=launch_dir,
                workdir=workdir if workdir != launch_dir else None,
                max_turns=turns,
                max_budget_usd=round(remaining, 2),
                disallowed_tools=() if writes else launch.WRITE_TOOLS,
                allowed_tools=options.allow_tools,
                extra_dirs=tuple(workspace.repo_dir / rel for rel in workspace.links
                                 if (workspace.repo_dir / rel).is_dir()) if workspace else (),
                mcp_servers=selection.servers,
                append_system_prompt=selection.system_prompt(),
                env={"CAUCE_WORKER_TASK": str(task["id"]), "CAUCE_WORKER_ATTEMPT": str(len(attempts) + 1)},
            )
            store.update_task(task["id"], current_cell=cell.label)
            store.add_event(task["id"], "attempt_started", seq=len(attempts) + 1, cell=cell.label,
                            model=spec.model_id or cell.model,
                            max_turns=turns, budget_usd=spec.max_budget_usd, capabilities=list(selection.names))
            result = launcher(spec)
            if result.passed and options.verify:
                result = _verified(result, options.verify, workdir)
            elif _settles(result, options.verify) and (
                    isolate.has_changes(workspace) if workspace else bool(result.changed_paths)):
                result = _settled(result, options.verify, workdir)
            changed.update(result.changed_paths)
            report.cells.append(cell.label)
            report.served.append(result.served_model)
            report.cost_usd += result.cost_usd
            seq = store.add_attempt(
                task["id"], cell=cell.label, max_turns=turns, passed=int(result.passed),
                failure=result.failure.value if result.failure else None, summary=result.summary,
                evidence=result.evidence[:4000], cost_usd=result.cost_usd,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens, turns=result.turns,
                duration_s=result.duration_s, changed_paths=result.changed_paths,
                served_model=result.served_model or None,
                capabilities=selection.names,
            )
            store.add_event(task["id"], "attempt_finished", seq=seq, cell=cell.label, passed=result.passed,
                            failure=result.failure.value if result.failure else None, cost_usd=result.cost_usd,
                            turns=result.turns, summary=result.summary[:500], denied=list(result.denied),
                            allow=list(result.allow), served_model=result.served_model,
                            changed=list(result.changed_paths)[:50])
            attempt = Attempt(cell, turns, result.passed, result.failure, result.summary, result.denied,
                              allow=result.allow, changed=result.changed_paths, evidence=result.evidence[-1500:])
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
            to = decision.cell.label if decision.cell and decision.continues else None
            report.moves.append(f"{decision.move.value}" + (f" to {to}" if to else "") + f": {decision.reason}")
            report.decisions.append(decision)
            store.add_event(task["id"], "moved", seq=seq, move=decision.move.value, reason=decision.reason,
                            next_cell=decision.cell.label if decision.cell else None,
                            from_cell=cell.label, to_cell=to, axis=decision.axis,
                            trigger=(result.failure or Failure.INCONCLUSIVE).value,
                            because=list(decision.because), skipped=list(decision.skipped),
                            turns_from=turns, turns_to=decision.max_turns if decision.continues else None,
                            budget_left=round(options.budget_usd - report.cost_usd, 4))
            if not decision.continues:
                # The worker's own account goes with the reason: "the task is
                # wrong" is only actionable next to what it found wrong.
                report.status = _FINAL_STATUS[decision.move]
                report.summary = f"{decision.reason}. The last worker's own account: {result.summary}" if (
                    result.summary) else decision.reason
                report.stop = stops.Stop(
                    _cause(decision, attempt), decision.reason, denied=attempt.denied, allow=attempt.allow,
                    attempt=seq,
                    next_cell=decision.cell.label if decision.cell else None, account=result.summary[:2000])
                break
            if decision.move is Move.NEXT_MODEL and workspace is not None:
                isolate.reset(workspace)
            cell, turns = decision.cell, decision.max_turns
    except Cancelled:
        report.stop = _cancelled(store, task["id"], len(attempts) or None)
        report.status, report.summary = "cancelled", report.stop.reason
    except KeyboardInterrupt:
        report.stop = stops.Stop("cancelled", "you cancelled it with Ctrl-C before it finished", by="you",
                                 attempt=len(attempts) or None, extra={"via": "keyboard"})
        report.status, report.summary = "cancelled", report.stop.reason
    finally:
        if workspace is not None:
            report.isolated = True
            report.changed = list(isolate.changed(workspace))
            unverified = "" if report.status == "done" else f" {isolate.UNVERIFIED}, {report.status})"
            # Work that outgrew its turns is kept too: the task was not wrong, only big.
            outgrew = report.stop is not None and report.stop.cause == "turns"
            report.branch = isolate.finish(
                workspace, keep=report.status in KEEPS_WORK or outgrew,
                message=f"cauce task #{task['id']}{unverified}: {task['title']}",
            )
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
        if status != "done" and store.get_task(task["id"])["dispatched"]:
            store.pause_lane(key, f"task #{task['id']} ended {status}: {report.summary[:200]}")
        if workspace is None:
            report.changed = sorted(changed)
        store.add_event(task["id"], "finished", status=status, final_cell=report.final_cell,
                        cost_usd=round(report.cost_usd, 4), branch=report.branch, impact=report.impact,
                        changed=report.changed[:50], stop=report.stop.data() if report.stop else None)
        store.add_message(task["id"], "worker", report.text())
    return report


#: Words that say a task needs a browser, or servers that keep running.
_BROWSER = re.compile(r"\b(browser|navegador|navigate|navega\w*|abr[ie]\w* (?:el )?navegador|screenshot|"
                      r"captura de pantalla|playwright|puppeteer|e2e)\b", re.IGNORECASE)
_SERVER = re.compile(r"(\bdev servers?\b|\blevanta\w*\b|\bstart (?:both |the )?(?:dev )?servers?\b|"
                     r"\bng serve\b|\bnpm (?:run )?(?:start|dev)\b|localhost:\d+)", re.IGNORECASE)
_BROWSER_CAPS = ("browser", "playwright", "chrome", "puppeteer")


def _unmet(text: str, registry: Mapping[str, caps.Capability]) -> list[str]:
    """What a task asks for that a one-shot worker does not have, said before any
    money is spent: a worker has no browser unless a capability gives it one,
    and a server it starts ends with it."""
    notes = []
    if _BROWSER.search(text) and not any(k in name.lower() for name in registry for k in _BROWSER_CAPS):
        notes.append("this task needs a browser and workers have none: register a browser MCP server in "
                     "your cauce capabilities.json (`cauce capabilities --example` has one) and allow its tools "
                     "(--allow 'mcp__browser'), or check the pages yourself")
    if _SERVER.search(text):
        notes.append("this task needs running servers: start them yourself before dispatching, or pass the "
                     "--allow rules that start them; a server a worker starts stops when it finishes")
    return notes


def _refused_here(store: Store, key: str | None, granted: Sequence[str]) -> list[tuple[str, int]]:
    """The rules workers in this repository lacked most often, past refusals read
    back to rules when an older run did not record them, the granted ones aside."""
    if not key:
        return []
    counts: dict[str, int] = {}
    for row in store.refusals(key):
        for rule in dict.fromkeys(row["allow"] or allow_rules.from_refusals(row["denied"])):
            if rule not in granted:
                counts[rule] = counts.get(rule, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]


def _cause(decision: Decision, attempt: Attempt) -> str:
    """What ended the run, from the move and the failure that led to it."""
    if decision.move is Move.BLOCKED:
        return "permission" if attempt.failure is Failure.PERMISSION else "environment"
    if decision.move is Move.NEEDS_APPROVAL:
        return "approval"
    if decision.move is Move.REPLAN:
        if attempt.failure in (Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG):
            return "spec"
        return "turns" if attempt.failure is Failure.TURNS_EXHAUSTED else "ladder"
    return "exhausted"


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
        parts.append("This task is read-only: find out and report. Do not change files. If it asks for a "
                     "change, you cannot make it here: answer `fail` with `spec_bug` and say so.")
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
            if a.denied:
                lines.append(f"    refused: {', '.join(a.denied[:5])}")
        last = attempts[-1]
        if not last.passed and last.evidence.strip():
            lines.append("  The output that failed the last attempt, last lines:\n" + last.evidence.strip()[-1500:])
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


def _check(command: str, workdir: Path) -> tuple[int | None, str]:
    """The repository's own check: (exit code, or None on a timeout; its output)."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir), capture_output=True, text=True,  # noqa: S602
                              timeout=VERIFY_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()[-3000:]


def _verified(result: launch.WorkerResult, command: str, workdir: Path) -> launch.WorkerResult:
    """A claimed pass checked by the repository's own command. The worker does not
    grade itself: a red check turns the pass into a code bug, with the check's
    output as the evidence the next attempt reads."""
    code, output = _check(command, workdir)
    if code is None:
        return dataclasses.replace(result, passed=False, failure=Failure.INCONCLUSIVE,
                                   summary=f"`{command}` timed out after the claimed pass")
    if code == 0:
        return result
    return dataclasses.replace(
        result, passed=False, failure=Failure.CODE_BUG, evidence=output,
        summary=f"claimed a pass, but `{command}` exited {code}: {result.summary}",
    )


#: Attempts that stopped before the worker could check its own work, through no
#: verdict of its own: the repository's check is run for them.
_UNCHECKED = frozenset({Failure.TURNS_EXHAUSTED, Failure.BUDGET_EXHAUSTED})


def _settles(result: launch.WorkerResult, command: str | None) -> bool:
    """A worker that could not run the command that would show its work — refused
    it, or ran out of turns or money first — and did not call it a failure: on a
    task that has written something, the repository's own check can still
    decide. Its claim alone never would. Run by cauce, the check costs the
    worker nothing, and a red one hands the next attempt the failing output."""
    if not command or result.passed:
        return False
    if result.failure is Failure.PERMISSION:
        return result.verdict in ("inconclusive", "pass")
    return result.failure in _UNCHECKED


def _settled(result: launch.WorkerResult, command: str, workdir: Path) -> launch.WorkerResult:
    code, output = _check(command, workdir)
    if result.failure is Failure.PERMISSION:
        cause = f"the worker was refused {', '.join(result.denied[:3])}"
    elif result.failure is Failure.TURNS_EXHAUSTED:
        cause = "the worker ran out of turns before checking its work"
    else:
        cause = "the worker reached its spending cap before checking its work"
    if code != 0:
        why = "timed out" if code is None else f"exited {code}"
        return dataclasses.replace(result, evidence=f"`{command}` {why}:\n{output}".strip(),
                                   summary=f"{result.summary} ({cause}; cauce ran `{command}`, which {why})")
    return dataclasses.replace(
        result, passed=True, failure=None, evidence=output or f"`{command}` exited 0",
        summary=f"{result.summary} ({cause}; `{command}` passed in its place)",
    )


def _remember(store: Store, task: dict, key: str | None, attempt: Attempt, outcome: str,
              result: launch.WorkerResult) -> None:
    problem = store.open_problem(task["title"], repo=key, symptom=task["body"][:1000])
    store.add_fix(
        problem, f"{attempt.cell.label}: {result.summary}", outcome, repo=key,
        why=str(attempt.failure or "") if outcome == "failed" else "",
        evidence=result.evidence, task_id=task["id"],
    )
