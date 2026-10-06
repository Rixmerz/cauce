"""Work that is waiting: the queue, its lanes, the dispatcher and the sweep.

A queued task costs nothing until it runs, and it runs in a worker — never in
the orchestrating session's context. Each repository is one lane. The first
task of an idle lane starts; one queued behind running work starts beside it
only when Haiku judged it independent of everything running and queued ahead
(`dispatch.decide`, kept on the task with its reason, `parallel` setting) —
otherwise it waits its turn. A task that ends in anything but a pass pauses
the lane, so nothing new starts on a state it may have left broken. A person
unpauses it.

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
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cauce import config, dispatch, stops
from cauce.matrix import Cell
from cauce.orchestrate import Options, Report
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
        # A session's prompt keeps the answer it had so far as its result.
        stops.record(store, task["id"], stops.Stop("died", gone), pid=None, current_cell=None,
                     result=task["source"] == "cauce")
        if task["dispatched"]:
            store.pause_lane(task["repo"], f"task #{task['id']} stopped with no sign of life")
        swept.append(task["id"])
    return swept


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
    raw = json.loads(task.get("options") or "{}")
    return Options(
        budget_usd=float(raw.get("budget_usd", defaults.budget_usd)),
        max_attempts=int(raw.get("max_attempts", defaults.max_attempts)),
        verify=raw.get("verify", defaults.verify),
        kind=raw.get("kind", defaults.kind),
        start=Cell.parse(raw["start"]) if raw.get("start") else defaults.start,
        allow_approval=bool(raw.get("allow_approval", defaults.allow_approval)),
        livespec=raw.get("livespec", defaults.livespec),
        use_model_classifier=defaults.use_model_classifier,
        allow_tools=tuple(raw.get("allow_tools") or defaults.allow_tools),
    )


Decide = Callable[[dict, list[dict]], dispatch.Decision]


def runnable(store: Store, repo: str | None, *, parallel: bool, decide: Decide) -> list[dict]:
    """What may start now. Per open lane: the first task when nothing runs; after
    that, only tasks judged independent of everything running and queued ahead,
    up to `MAX_PARALLEL` at once. A task not judged yet is judged here, once."""
    picked = []
    for lane in store.lanes():
        if lane["paused"] or not lane["queued"] or (repo is not None and lane["repo"] != repo):
            continue
        running = store.dispatched_running(lane["repo"])
        ahead, mine = list(running), 0
        for task in store.queued_in(lane["repo"]):
            if len(running) + mine >= MAX_PARALLEL:
                break
            if running or mine:
                if not parallel:
                    break
                if task["parallel"] is None:
                    decision = decide(task, ahead)
                    store.set_parallel(task["id"], decision.parallel, decision.reason)
                    if decision.cost_usd:
                        store.update_task(task["id"], cost_usd=(task["cost_usd"] or 0) + decision.cost_usd)
                    task = {**task, "parallel": int(decision.parallel)}
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
                else "nothing runnable is queued (empty, every lane paused, or waiting on work in flight)")
            return report
        pause()
