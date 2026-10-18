"""Work that is waiting: the queue, its lanes, the dispatcher and the sweep.

A queued task costs nothing until it runs, and it runs in a worker — never in
the orchestrating session's context. Each repository is one lane. The first
task of an idle lane starts; one queued behind running work that will not write
(a reading kind, or text that says it changes nothing) starts beside it with no
question; any other starts beside it only when Haiku judged it independent of
all the writing work running and queued ahead (`dispatch.decide`, kept on the
task with its reason, `parallel` setting) — otherwise it waits its turn. A
task that ends in anything but a pass pauses
the lane, so nothing new starts on a state it may have left broken — unless a
person cancelled it, which is their call on that task, not on the queue. A
person unpauses it.

Each dispatched task runs as its own `cauce run-queued` process: its pid is its
own, so the sweep and `cauce cancel` treat it like any other run, and the
dispatcher only watches.

The sweep is what keeps "running" honest. A `cauce run` that was killed leaves
its task marked running forever; the sweep checks the process, and a task whose
driver is gone becomes interrupted and pauses its lane. It runs from hooks, the
dispatcher and the UI server — never from a page being open.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cauce import after, classify, config, dispatch, isolate, stops, surfaces
from cauce.matrix import READ_ONLY_KINDS, Cell
from cauce.orchestrate import Options, Report, declares_read_only
from cauce.store import Store, home

#: How long a session prompt may sit in `running` with no update before it is
#: taken for abandoned. Claude Code fires no Stop for an interrupted turn.
SESSION_TASK_TIMEOUT = timedelta(hours=12)
DEFAULT_MAX_TASKS = 10
#: Tasks the dispatcher runs at once in one repository.
MAX_PARALLEL = 3
#: How often a dispatcher with work out checks on it and on new arrivals.
POLL_S = 2.0

Runner = Callable[..., Report]


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is someone else's
    return True


def sweep(store: Store, *, now: datetime | None = None, is_alive: Callable[[int | None], bool] = alive) -> list[int]:
    """Mark running tasks with no sign of life as interrupted. Returns their ids."""
    now = now or datetime.now(UTC)
    swept = []
    for task in store.list_tasks(status=["running"], limit=500):
        if task["source"] == "cauce":
            dead = not is_alive(task["pid"])
        else:
            updated = datetime.fromisoformat(task["updated_at"])
            dead = task["source"] != "delegation" and now - updated > SESSION_TASK_TIMEOUT
        if not dead:
            continue
        gone = (f"no sign of life: its process ({task['pid']}) is gone" if task["source"] == "cauce" and task["pid"]
                else "no sign of life")
        # A session's turn is no work to resume: the session closed or was cut off
        # before its answer arrived. It keeps the answer it had so far as its result.
        stop = (stops.Stop("turn", "the session's turn ended without its answer reaching cauce")
                if task["source"] == "hook" else stops.Stop("died", gone))
        stops.record(store, task["id"], stop, pid=None, current_cell=None, result=task["source"] == "cauce")
        if task["dispatched"]:
            store.pause_lane(task["repo"], f"task #{task['id']} stopped with no sign of life")
        swept.append(task["id"])
    return swept


#: Statuses whose branch waits on nothing: no resume continues from it.
PRUNE_STATUSES = frozenset({"done", "dismissed"})


def prune(store: Store, dirs: list[Path] | None = None, *, dry_run: bool = False) -> list[dict]:
    """Delete the branches cauce kept whose work landed, in each checkout its
    tasks ran in (or in `dirs`): merged, squash-merged, or tool artifacts only;
    see `isolate.prunable`. Each is recorded on its task with its tip, so
    `git branch <name> <tip>` brings it back. Off with `cauce config prune off`."""
    from cauce import config, isolate, repo

    if not dry_run and not config.enabled("prune", os.environ):
        return []
    if dirs is None:
        seen: dict[str, Path] = {}
        for task in store.list_tasks(status=sorted(PRUNE_STATUSES), source=("cauce", "queue"), limit=2000):
            where = Path(task["cwd"]) if task.get("cwd") else None
            top = repo.toplevel(where) if where is not None and where.is_dir() else None
            if top is not None:
                seen.setdefault(str(top), top)
        dirs = list(seen.values())
    pruned = []
    for top in dirs:
        kept = isolate.kept_branches(top)
        trees = {task_id: isolate.tree_of(top, tip) for task_id, tip in kept.items()}
        for task_id, tip in sorted(kept.items()):
            task = store.get_task(task_id)
            if task is None or task["status"] not in PRUNE_STATUSES:
                continue
            branch = isolate.branch_for(task_id)
            why = isolate.prunable(top, branch)
            if why is None:
                # Another kept branch holds the very same work: this copy loses nothing.
                twin = next((other for other in sorted(trees) if other != task_id and trees[other]
                             and trees[other] == trees[task_id]), None)
                why = f"the same work as {isolate.branch_for(twin)}" if twin is not None else None
            if why is None or (not dry_run and not isolate.delete_branch(top, branch)):
                continue
            trees.pop(task_id, None)
            if not dry_run:
                store.add_event(task_id, "branch_pruned", branch=branch, tip=tip, why=why, repo=str(top))
            pruned.append({"task": task_id, "branch": branch, "tip": tip, "why": why, "repo": str(top)})
    return pruned


@dataclass(frozen=True)
class Ran:
    task_id: int
    status: str


@dataclass
class WorkReport:
    ran: list[Ran] = field(default_factory=list)
    stopped_because: str = ""

    def text(self) -> str:
        lines = [f"task #{r.task_id}: {r.status}" for r in self.ran]
        lines.append(self.stopped_because)
        return "\n".join(lines)


def queued_options(task: dict, defaults: Options) -> Options:
    """The options a queued or resumed task runs with. Keys an older cauce stored
    and nothing reads any more (a dollar `budget_usd`) are left where they are."""
    raw = json.loads(task.get("options") or "{}")
    return Options(
        max_attempts=int(raw.get("max_attempts", defaults.max_attempts)),
        verify=raw.get("verify", defaults.verify),
        kind=raw.get("kind", defaults.kind),
        start=Cell.parse(raw["start"]) if raw.get("start") else defaults.start,
        allow_approval=bool(raw.get("allow_approval", defaults.allow_approval)),
        livespec=raw.get("livespec", defaults.livespec),
        use_model_classifier=defaults.use_model_classifier,
        allow_tools=tuple(raw.get("allow_tools") or defaults.allow_tools),
        isolate=defaults.isolate and not raw.get("no_isolate"),
        max_turns=int(raw["max_turns"]) if raw.get("max_turns") else defaults.max_turns,
        base=raw.get("base") or defaults.base,
        role=raw.get("role") or defaults.role,
        step_role=raw.get("step_role") or defaults.step_role,
        surfaces=tuple(raw.get("surfaces") or defaults.surfaces),
        ledger=raw.get("ledger") or defaults.ledger,
        keep_learned=bool(raw.get("keep_learned", defaults.keep_learned)),
        carry=bool(raw.get("carry", defaults.carry)),
        add_dirs=tuple(raw.get("add_dirs") or defaults.add_dirs),
        services=tuple(raw.get("services") or defaults.services),
        run_verify=raw.get("run_verify") or defaults.run_verify,
    )


Decide = Callable[[dict, list[dict]], dispatch.Decision]


def by_surfaces(task: dict, ahead: list[dict]) -> dispatch.Decision | None:
    """The call surfaces settle without a model. The task waits when the surfaces it
    declared may touch those another task declared, or hold a file a running task
    already changed (`changed`, from git), whatever the others declare; it starts
    beside the work ahead when every task involved declares surfaces and they cannot
    touch. None otherwise: then the dispatcher's judgment decides."""
    mine = dispatch.declared_surfaces(task)
    theirs = [(t, dispatch.declared_surfaces(t)) for t in ahead]
    for t, declared in theirs if mine else ():
        if declared and not surfaces.disjoint(mine, declared):
            return dispatch.Decision(False, f"its surfaces may touch those of #{t['id']}")
        touched = next((p for p in t.get("changed") or () if surfaces.allows(mine, p)), None)
        if touched is not None:
            return dispatch.Decision(False, f"#{t['id']} already changed {touched}, inside its surfaces")
    if not mine or not theirs or any(not s for _, s in theirs):
        return None
    return dispatch.Decision(True, "its surfaces cannot touch those of the work ahead")


def with_changes(store: Store, task: dict) -> dict:
    """A running task with the files git shows it changed so far in its worktree, as
    `changed`; as it is when git cannot say (no worktree, no attempt started yet)."""
    if not task.get("cwd"):
        return task
    started = store.last_event(task["id"], "attempt_started")
    files = isolate.changed_so_far(home(), Path(task["cwd"]), task["id"], started["data"] if started else {})
    return task if files is None else {**task, "changed": list(files)}


def _behind(decision: dispatch.Decision, ahead: list[dict]) -> int | None:
    """The task a held-back one waits for: the one its reason names, else the first ahead of it."""
    if decision.parallel or not ahead:
        return None
    ids = [t["id"] for t in ahead]
    return next((int(n) for n in re.findall(r"#(\d+)", decision.reason) if int(n) in ids), ids[0])


#: Why a task that changes nothing starts beside writing work with no question asked.
READS_ONLY = "reads only: runs beside writing work"
#: Why a task that writes starts beside work that does not.
NOTHING_WRITES_AHEAD = "nothing that writes runs or waits ahead of it"


def reads_only(task: dict) -> bool:
    """Whether a task will not write: its kind is a reading one, or its own words
    say it changes nothing (the rule `orchestrate.run` applies once it has a kind).
    A task with no kind yet gets the one the run would give it without the model:
    a text no rule reads, or one that asks for a change, is a build and writes."""
    try:
        chosen = json.loads(task.get("options") or "{}").get("kind") or task.get("kind")
    except ValueError:
        chosen = task.get("kind")
    text = task.get("body") or task.get("title") or ""
    kind = chosen or classify.classify(text, use_model=False).kind
    return kind in READ_ONLY_KINDS or declares_read_only(kind, text)


def _judged(store: Store, task: dict, ahead: list[dict], decide: Decide,
            read_changes: Callable[[], None]) -> dict:
    """The queued task with its dispatch decision made. A task that will not write
    is never put to the model, and a writing one is judged against the writing
    work ahead of it alone: a reader changes no file and holds no checkout, and the
    goal its text carries is not what it does. With no writing work ahead it starts,
    whatever a "no" given against work that has ended once held it behind.
    `read_changes` puts what git shows the running tasks changed into `ahead`,
    called only when a task is put to a decision."""
    if reads_only(task):
        if not task["parallel"]:  # a "no" given before this rule held a reader behind a build
            store.set_parallel(task["id"], True, READS_ONLY)
        return {**task, "parallel": 1}
    writing = [t for t in ahead if not reads_only(t)]
    if not writing:
        if not task["parallel"]:
            store.set_parallel(task["id"], True, NOTHING_WRITES_AHEAD)
        return {**task, "parallel": 1}
    if task["parallel"] is not None:
        return task
    read_changes()
    writing = [t for t in ahead if not reads_only(t)]
    settled = by_surfaces(task, writing)
    decision = settled or decide(task, writing)
    store.set_parallel(task["id"], decision.parallel, decision.reason)
    store.add_event(task["id"], "dispatch_decided", parallel=decision.parallel,
                    reason=decision.reason, behind=_behind(decision, writing),
                    ahead=[t["id"] for t in writing], by="surfaces" if settled else "haiku")
    if decision.cost_usd:
        store.update_task(task["id"], cost_usd=(task["cost_usd"] or 0) + decision.cost_usd)
    return {**task, "parallel": int(decision.parallel)}


def runnable(store: Store, repo: str | None, *, parallel: bool, decide: Decide) -> list[dict]:
    """What may start now. Per open lane: the first task when nothing runs; after
    that, tasks that will not write, and tasks judged independent of the writing
    work running and queued ahead, up to `MAX_PARALLEL` at once. A task not
    judged yet is judged here, once. Work held behind other work (`after`) is
    never picked, and nothing waits behind it."""
    picked = []
    for lane in store.lanes():
        if lane["paused"] or not lane["queued"] or (repo is not None and lane["repo"] != repo):
            continue
        running = store.dispatched_running(lane["repo"])
        ahead, mine, read = list(running), 0, False

        def read_changes(running: list[dict] = running, ahead: list[dict] = ahead) -> None:
            nonlocal read
            if not read:
                # Git is read only when a task is to be judged, and once a pass.
                ahead[:len(running)] = [with_changes(store, t) for t in running]
                read = True

        for task in store.queued_in(lane["repo"]):
            if after.held(task):
                continue  # it waits for other work to pass, not for a place in the lane
            if len(running) + mine >= MAX_PARALLEL:
                break
            if running or mine:
                if not parallel:
                    break
                task = _judged(store, task, ahead, decide, read_changes)
                if not task["parallel"]:
                    ahead.append(task)
                    continue
            picked.append(task)
            ahead.append(task)
            mine += 1
    return picked


class Handle(Protocol):
    task_id: int

    def poll(self) -> str | None:
        """The task's status once it ended, None while it runs."""


class _Done:
    """A run that already finished: the in-process runner the tests use."""

    def __init__(self, task_id: int, status: str) -> None:
        self.task_id, self.status = task_id, status

    def poll(self) -> str | None:
        return self.status


class _Process:
    """`cauce run-queued <id>` as its own process."""

    def __init__(self, task: dict, where: Path, store: Store, defaults: Options) -> None:
        self.task_id, self.store = task["id"], store
        argv = [sys.executable, "-m", "cauce", "run-queued", str(task["id"])]
        if not defaults.use_model_classifier:
            argv.append("--no-model")
        log = home() / "work" / f"task-{task['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as out:
            self.proc = subprocess.Popen(argv, cwd=str(where), env=dispatch.child_env(),
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)

    def poll(self) -> str | None:
        if self.proc.poll() is None:
            return None
        task = self.store.get_task(self.task_id)
        if task is None:
            return "failed"
        if task["status"] in ("running", "queued"):
            # It exited without saying how it ended: it died.
            stops.record(self.store, self.task_id, stops.Stop(
                "died", f"its process exited with {self.proc.returncode} without saying how the task ended"),
                pid=None, current_cell=None)
            self.store.pause_lane(task["repo"], f"task #{self.task_id}: its process exited unexpectedly")
            return "interrupted"
        return task["status"]


Start = Callable[[dict, Path], Handle]


def work(
    store: Store,
    *,
    repo: str | None = None,
    max_tasks: int = DEFAULT_MAX_TASKS,
    defaults: Options | None = None,
    runner: Runner | None = None,
    parallel: bool | None = None,
    decide: Decide | None = None,
    start: Start | None = None,
    pause: Callable[[], None] | None = None,
    **run_kwargs,
) -> WorkReport:
    """Run queued work until nothing is runnable and nothing is out, or
    `max_tasks` started — the bound on how long an unattended run goes on.

    `runner` runs a task in this process, one after another (tests); without it,
    each task is its own process and several may run at once.
    """
    defaults = defaults or Options()
    parallel = config.enabled("parallel", os.environ) if parallel is None else parallel
    decide = decide or (lambda task, ahead: dispatch.decide(task, ahead, cwd=home()))
    pause = pause or (lambda: time.sleep(POLL_S))
    if start is None:
        if runner is not None:
            def start(task: dict, where: Path) -> Handle:
                ran = runner(task["body"], where, store, queued_options(task, defaults), task_id=task["id"],
                             **run_kwargs)
                return _Done(task["id"], ran.status)
        else:
            def start(task: dict, where: Path) -> Handle:
                return _Process(task, where, store, defaults)
    report, out, started = WorkReport(), [], 0
    sweep(store)
    while True:
        for handle in list(out):
            status = handle.poll()
            if status is not None:
                out.remove(handle)
                report.ran.append(Ran(handle.task_id, status))
        launched = 0
        if started < max_tasks:
            for task in runnable(store, repo, parallel=parallel, decide=decide)[:max_tasks - started]:
                if not store.claim(task["id"]):
                    continue  # another dispatcher took it
                where = Path(task["cwd"]) if task["cwd"] and Path(task["cwd"]).is_dir() else None
                if where is None:
                    stops.record(store, task["id"], stops.Stop(
                        "missing_dir", f"its directory no longer exists: {task['cwd'] or '(none recorded)'}"))
                    store.pause_lane(task["repo"], f"task #{task['id']}: its directory no longer exists")
                    continue
                started += 1
                launched += 1
                out.append(start(task, where))
        if launched:
            continue  # look again at once: a finished run may have opened a lane
        if not out:
            report.stopped_because = (
                f"started {max_tasks} tasks, the unattended limit; run again to continue" if started >= max_tasks
                else "nothing runnable is queued (empty, every lane paused, waiting on work in flight, or held "
                     "until other work passes)")
            return report
        pause()
