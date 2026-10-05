"""Work that is waiting: the queue, its lanes, the dispatcher and the sweep.

A queued task costs nothing until it runs, and it runs in a worker — never in
the orchestrating session's context. Each repository is one serial lane: the
dispatcher runs its tasks one after another, and a task that ends in anything
but a pass pauses the lane, so the next task never starts on a state the last
one left broken. A person unpauses it.

The sweep is what keeps "running" honest. A `cauce run` that was killed leaves
its task marked running forever; the sweep checks the process, and a task whose
driver is gone becomes interrupted and pauses its lane. It runs from hooks, the
dispatcher and the UI server — never from a page being open.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cauce.matrix import Cell
from cauce.orchestrate import Options, Report
from cauce.orchestrate import run as run_task
from cauce.store import Store

#: How long a session prompt may sit in `running` with no update before it is
#: taken for abandoned. Claude Code fires no Stop for an interrupted turn.
SESSION_TASK_TIMEOUT = timedelta(hours=12)
DEFAULT_MAX_TASKS = 10

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
        store.update_task(task["id"], status="interrupted", pid=None, current_cell=None)
        store.add_event(task["id"], "finished", status="interrupted", reason="no sign of life")
        if task["dispatched"]:
            store.pause_lane(task["repo"], f"task #{task['id']} stopped with no sign of life")
        swept.append(task["id"])
    return swept


@dataclass
class WorkReport:
    ran: list[Report] = field(default_factory=list)
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
    )


def work(
    store: Store,
    *,
    repo: str | None = None,
    max_tasks: int = DEFAULT_MAX_TASKS,
    defaults: Options | None = None,
    runner: Runner = run_task,
    **run_kwargs,
) -> WorkReport:
    """Drain lanes until the queue is empty, every lane is paused or busy, or
    `max_tasks` ran — the bound on how long an unattended run goes on."""
    defaults = defaults or Options()
    report = WorkReport()
    sweep(store)
    while len(report.ran) < max_tasks:
        task = store.next_queued(repo)
        if task is None:
            report.stopped_because = "nothing runnable is queued (empty, or every lane is paused or busy)"
            return report
        if not store.claim(task["id"]):
            continue  # another dispatcher took it
        where = Path(task["cwd"]) if task["cwd"] and Path(task["cwd"]).is_dir() else None
        if where is None:
            store.update_task(task["id"], status="blocked", result="its directory no longer exists")
            store.pause_lane(task["repo"], f"task #{task['id']}: its directory no longer exists")
            continue
        report.ran.append(runner(task["body"], where, store, queued_options(task, defaults),
                                 task_id=task["id"], **run_kwargs))
    report.stopped_because = f"ran {max_tasks} tasks, the unattended limit; run again to continue"
    return report
