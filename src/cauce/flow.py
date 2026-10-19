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

How many run at once is no number fixed here: it is the machine's level
(`cauce.capacity`), which adapts to the API and the machine and is shared between
the lanes by reserve and lending. Every dispatcher settles it, allots it and
claims what it starts under one lock.

Each dispatched task runs as its own `cauce run-queued` process: its pid is its
own, so the sweep and `cauce cancel` treat it like any other run, and the
dispatcher only watches.

The sweep is what keeps "running" honest. A `cauce run` that was killed leaves
its task marked running forever; the sweep checks the process, and a task whose
driver is gone becomes interrupted and pauses its lane. It runs from hooks, the
dispatcher and the UI server — never from a page being open.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cauce import after, capacity, classify, config, dispatch, isolate, launch, stops, surfaces
from cauce.matrix import READ_ONLY_KINDS, Cell
from cauce.orchestrate import Options, Report, declares_read_only
from cauce.store import Store, home

#: How long a session prompt may sit in `running` with no update before it is
#: taken for abandoned. Claude Code fires no Stop for an interrupted turn.
SESSION_TASK_TIMEOUT = timedelta(hours=12)
DEFAULT_MAX_TASKS = 10
#: How often a dispatcher with work out checks on it and on new arrivals.
POLL_S = 2.0
#: Its waits, one after another, while the disk is full: it keeps its lock and the work it
#: has out, and goes on when the store takes writes again (the last wait repeats).
DISK_WAITS_S = (5, 15, 30, 60, 120, 300)
#: How long it waits for space in all before it stops; a hook starts a fresh one later
#: (`dispatch.revive`).
DISK_GIVE_UP_S = 3600

Runner = Callable[..., Report]


def disk_full(exc: BaseException) -> bool:
    """Whether an error is the disk running out: SQLite's "database or disk is full" or
    "disk I/O error", or an OS write refused for space. Any other error is cauce's own."""
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        said = str(exc).lower()
        return "disk is full" in said or "disk i/o error" in said
    return False


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
    # It stopped on `max_tasks` with work still queued in its lane: a detached
    # dispatcher hands that work to a fresh one (`cli.cmd_work`), never leaves it.
    at_limit: bool = False

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
        parts=bool(raw.get("parts", defaults.parts)),
        integrate=raw.get("integrate") or defaults.integrate,
        touches=tuple(raw.get("touches") or defaults.touches),
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


def _walk(store: Store, repo: str | None, *, parallel: bool, decide: Decide) -> Iterator[dict]:
    """A lane's tasks that may start now, in queue order, each judged only when it is
    reached: the first when nothing runs; after that, tasks that will not write, and
    tasks judged independent of the writing work running and queued ahead. Work held
    behind other work (`after`) is never picked, and nothing waits behind it."""
    running = store.dispatched_running(repo)
    ahead, mine, read = list(running), 0, False

    def read_changes() -> None:
        nonlocal read
        if not read:
            # Git is read only when a task is to be judged, and once a pass.
            ahead[:len(running)] = [with_changes(store, t) for t in running]
            read = True

    for task in store.queued_in(repo):
        if after.held(task):
            continue  # it waits for other work to pass, not for a place in the lane
        if running or mine:
            if not parallel:
                return
            task = _judged(store, task, ahead, decide, read_changes)
            if not task["parallel"]:
                ahead.append(task)
                continue
        ahead.append(task)
        mine += 1
        yield task


def _demand(queued: list[dict], running: int, *, parallel: bool) -> int:
    """What another dispatcher's lane could start now, read without judging anything: its
    first task when nothing runs there, and beside running work every task that starts
    without a decision (it will not write) or that a decision already let start. One not
    judged yet is no demand: a worker kept for it would sit idle if it is judged to wait."""
    idle = running == 0
    if not parallel:
        return 1 if idle and queued else 0
    return sum(1 for n, t in enumerate(queued) if (idle and n == 0) or t.get("parallel") or reads_only(t))


def _lanes(store: Store) -> tuple[list[capacity.Lane], dict[str | None, list[dict]]]:
    """Every lane as the allotment reads it, and the queued work of each that may run
    (none held behind other work). A lane whose work all runs has no queue row, yet holds
    its workers: it is read too."""
    counts = store.dispatched_counts()
    lanes, queued = [], {}
    for lane in store.lanes():
        key = lane["repo"] or None
        queued[key] = [t for t in store.queued_in(key) if not after.held(t)] if lane["queued"] else []
        lanes.append(capacity.Lane(key, counts.get(key, 0), bool(queued[key]),
                                   queued[key][0]["id"] if queued[key] else 0, bool(lane["paused"])))
    listed = {x.repo for x in lanes}
    lanes += [capacity.Lane(key, n, False, 0) for key, n in counts.items() if key not in listed]
    return lanes, queued


def shares(store: Store, level: int) -> dict[str | None, capacity.Share]:
    """Each lane's share of the level as it stands: what it reserves and runs, and what it
    lends or borrows. Nothing is judged or started."""
    return capacity.allot(level, _lanes(store)[0], lambda key: False)


def pick(store: Store, repo: str | None, *, parallel: bool, decide: Decide,
         level: int | None = None) -> tuple[list[dict], dict[str | None, capacity.Share]]:
    """What may start now within the machine's level, and each lane's share of it
    (`capacity.allot`): reserves first, in the order each lane's oldest work was queued,
    then what is lent. The lanes of `repo` (every lane, with None) are walked and judged
    (`_walk`); another dispatcher's are read as they stand, never judged here."""
    level = level if level is not None else capacity.current(home()).value
    lanes, queued = _lanes(store)
    walks: dict[str | None, Iterator[dict]] = {}
    demand: dict[str | None, int] = {}
    for lane in lanes:
        if lane.waiting and not lane.paused:
            if repo is None or lane.repo == repo:
                walks[lane.repo] = _walk(store, lane.repo, parallel=parallel, decide=decide)
            else:
                demand[lane.repo] = _demand(queued[lane.repo], lane.running, parallel=parallel)
    picked: list[dict] = []

    def take(key: str | None) -> bool:
        if key in walks:
            task = next(walks[key], None)
            if task is None:
                del walks[key]
                return False
            picked.append(task)
            return True
        if demand.get(key, 0) > 0:
            demand[key] -= 1
            return True
        return False

    shares = capacity.allot(level, lanes, take)
    return picked, shares


def runnable(store: Store, repo: str | None, *, parallel: bool, decide: Decide,
             level: int | None = None) -> list[dict]:
    """What may start now (`pick`), within the machine's level: the one kept, unless given."""
    return pick(store, repo, parallel=parallel, decide=decide, level=level)[0]


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
            # It exited without saying how it ended: it died. Killed outright, the kernel's
            # out-of-memory killer is the likely hand, and the machine's level halves on it.
            killed = self.proc.returncode in launch.KILLED
            stops.record(self.store, self.task_id, stops.Stop(
                "died", f"its process exited with {self.proc.returncode} without saying how the task ended"
                + (" (killed: the kernel does so when memory runs out)" if killed else ""),
                extra={"starved": "memory"} if killed else {}), pid=None, current_cell=None)
            self.store.pause_lane(task["repo"], f"task #{self.task_id}: its process exited unexpectedly")
            return "interrupted"
        return task["status"]


Start = Callable[[dict, Path], Handle]


def _settle(store: Store) -> None:
    """What ended settles the standards and the evolution history (`cauce.evolution`).
    Its failure is an event on task 0 and never stops the queue."""
    from cauce import evolution

    try:
        evolution.settle(store)
    except Exception as exc:  # the dispatcher goes on; the error is kept, never silent
        store.add_event(0, "evolution_error", error=f"{type(exc).__name__}: {exc}"[:300])


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
    machine: Callable[[], capacity.Machine] | None = None,
    backoff: Callable[[float], None] | None = None,
    **run_kwargs,
) -> WorkReport:
    """Run queued work until nothing is runnable and nothing is out, or
    `max_tasks` started — the bound on how long an unattended run goes on. Each
    pass settles the machine's level (`capacity.settle`, on `machine()`) first;
    a lane whose work waits only for a worker another lane holds keeps waiting.

    `runner` runs a task in this process, one after another (tests); without it,
    each task is its own process and several may run at once.

    A full disk does not end it (`disk_full`): it waits (`DISK_WAITS_S`, by `backoff`)
    with its work out and its lock held, tries the pass again, and once the store takes
    writes the level halves for it (`capacity.disk_filled`); after `DISK_GIVE_UP_S` it stops.
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
    machine = machine or (lambda: capacity.machine(home()))
    backoff = backoff or time.sleep
    ceiling = config.max_parallel(os.environ)
    report, out, started, wanting, waits = WorkReport(), [], 0, False, False
    full_for: float | None = None  # seconds waited for space, while the disk is full
    fails = 0

    def through() -> None:
        """A whole pass went through: once a full disk is behind it, the level halves for it."""
        nonlocal full_for, fails
        if full_for is not None:
            with capacity.held(home()):
                capacity.disk_filled(store, home(), waited_s=full_for, ceiling=ceiling)
            full_for, fails = None, 0

    try:
        sweep(store)
    except Exception as exc:
        if not disk_full(exc):
            raise
    while True:
        try:
            ended = False
            for handle in list(out):
                status = handle.poll()
                if status is not None:
                    out.remove(handle)
                    report.ran.append(Ran(handle.task_id, status))
                    ended = True
            if ended:
                _settle(store)
            launched: list[tuple[dict, Path]] = []
            if started < max_tasks:
                with capacity.held(home()):
                    level = capacity.settle(store, home(), machine=machine(), ceiling=ceiling, wanting=wanting)
                    picked, shares = pick(store, repo, parallel=parallel, decide=decide, level=level.value)
                    wanting = capacity.full(level.value, shares)
                    # Its own work waits for a worker another lane holds: it waits too, rather than stop.
                    waits = wanting and any(s.waiting for k, s in shares.items() if repo is None or k == repo)
                    for task in picked[:max_tasks - started]:
                        if not store.claim(task["id"]):
                            continue  # another dispatcher took it
                        where = Path(task["cwd"]) if task["cwd"] and Path(task["cwd"]).is_dir() else None
                        if where is None:
                            stops.record(store, task["id"], stops.Stop(
                                "missing_dir", f"its directory no longer exists: {task['cwd'] or '(none recorded)'}"))
                            store.pause_lane(task["repo"], f"task #{task['id']}: its directory no longer exists")
                            continue
                        started += 1
                        # When the dispatcher started it, to the millisecond: the run measures its
                        # pickup (from queued) and its spawn (to its plan) from here.
                        store.add_event(task["id"], "dispatched", at=round(time.time(), 3))
                        launched.append((task, where))
            for n, (task, where) in enumerate(launched):
                try:
                    out.append(start(task, where))
                except Exception as exc:
                    if disk_full(exc):  # its process never started: it goes back to the queue, with the rest
                        for back, _ in launched[n:]:
                            with contextlib.suppress(Exception):
                                store.update_task(back["id"], status="queued", dispatched=0)
                    raise
            through()
            if launched:
                continue  # look again at once: a finished run may have opened a lane
            if not out and (not waits or started >= max_tasks):
                report.at_limit = started >= max_tasks and bool(
                    store.queued_in(repo) if repo is not None else any(lane["queued"] for lane in store.lanes()))
                report.stopped_because = (
                    f"started {max_tasks} tasks, the unattended limit; run again to continue" if started >= max_tasks
                    else "nothing runnable is queued (empty, every lane paused, waiting on work in flight, or held "
                         "until other work passes)")
                return report
            pause()
        except Exception as exc:
            if not disk_full(exc):
                raise
            waited = full_for or 0.0
            if waited >= DISK_GIVE_UP_S:
                report.stopped_because = (f"the disk stayed full for {int(waited)}s ({exc}); the next session's hook "
                                          "starts a dispatcher again")
                return report
            delay = DISK_WAITS_S[min(fails, len(DISK_WAITS_S) - 1)]
            fails += 1
            backoff(delay)
            full_for = waited + delay
