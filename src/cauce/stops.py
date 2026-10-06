"""Why a task stopped short of a pass, who made that call, and what continues it.

A card that only says "blocked" asks a person to act without saying on what,
and "cancelled" does not say whether they did it. Every place that ends a task
without a pass — the run's own loop, `cauce cancel`, the UI, the dispatcher,
the sweep — writes one account on the task's `finished` event: the cause, who
or what made the call, the reason in full, and the command that continues it
when there is one.

Tasks that ended before these accounts existed get one read back from what
their events and attempts already say (`recovered`), marked as such: an
account cauce did not record is never presented as if it had.

Light on purpose: the UI, the hooks and the CLI read it.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

#: Who made the call, as the board says it. `you` is the only one a person did
#: by hand; `settings` and `budget` are theirs too, set before the run.
WHO = {
    "you": "you",
    "settings": "your permission settings",
    "budget": "the task's budget",
    "environment": "the environment",
    "worker": "the worker's verdict",
    "cauce": "cauce's rules",
    "outside": "a signal from outside cauce",
    "unknown": "not recorded",
}

#: cause → (status it ends in, who made the call).
CAUSES = {
    "permission": ("blocked", "settings"),
    "environment": ("blocked", "environment"),
    "budget": ("blocked", "budget"),
    "missing_dir": ("blocked", "environment"),
    "approval": ("needs_approval", "cauce"),
    "spec": ("replan", "worker"),
    "turns": ("replan", "cauce"),
    "ladder": ("replan", "cauce"),
    "exhausted": ("failed", "cauce"),
    "attempts": ("failed", "cauce"),
    "cancelled": ("cancelled", "you"),
    "signal": ("cancelled", "outside"),
    "died": ("interrupted", "cauce"),
    "crashed": ("failed", "cauce"),
}

#: How a person's cancel arrived, as the account says it.
CANCEL_VIA = {
    "ui": "from the UI",
    "cli": "with `cauce cancel`",
    "queue": "with `cauce queue rm`",
    "keyboard": "with Ctrl-C",
}


@dataclass(frozen=True)
class Stop:
    cause: str
    reason: str
    #: Overrides the cause's usual author, e.g. a ladder that ran out is cauce's
    #: call but a cancel's author is whoever sent it.
    by: str = ""
    #: The tool calls the settings refused, as rules `--allow` takes.
    denied: tuple[str, ...] = ()
    #: The cell a person must approve before the run goes on.
    next_cell: str | None = None
    #: The budget the run had, for the suggestion to raise it.
    budget_usd: float | None = None
    #: The attempt it stopped after, when one ran.
    attempt: int | None = None
    #: What the last worker said, beside cauce's reason: "the task is wrong" is
    #: only actionable next to what it found wrong.
    account: str = ""
    #: Read back from an older task's events, not recorded when it stopped.
    recovered: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return CAUSES.get(self.cause, ("failed", "cauce"))[0]

    @property
    def who(self) -> str:
        return self.by or CAUSES.get(self.cause, ("failed", "unknown"))[1]

    def line(self) -> str:
        return f"stopped by {WHO.get(self.who, self.who)}: {self.reason}"

    def data(self) -> dict[str, Any]:
        """For the `finished` event; `read` turns it back into a Stop."""
        out: dict[str, Any] = {"cause": self.cause, "by": self.who, "reason": self.reason}
        if self.denied:
            out["denied"] = list(self.denied)
        for key in ("next_cell", "budget_usd", "attempt"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.account:
            out["account"] = self.account
        if self.recovered:
            out["recovered"] = True
        return {**self.extra, **out}


def read(data: dict[str, Any] | None) -> Stop | None:
    if not isinstance(data, dict) or not data.get("cause"):
        return None
    known = {"cause", "by", "reason", "denied", "next_cell", "budget_usd", "attempt", "account", "recovered"}
    return Stop(str(data["cause"]), str(data.get("reason") or ""), by=str(data.get("by") or ""),
                denied=tuple(data.get("denied") or ()), next_cell=data.get("next_cell"),
                budget_usd=data.get("budget_usd"), attempt=data.get("attempt"),
                account=str(data.get("account") or ""), recovered=bool(data.get("recovered")),
                extra={k: v for k, v in data.items() if k not in known})


def next_step(task_id: int, stop: Stop, *, resumable: bool = True) -> str | None:
    """The command that continues the task, or None when none would: a task the
    worker found wrong is rewritten, and one that never ran cannot resume."""
    if not resumable or stop.cause in ("spec", "turns", "ladder", "missing_dir"):
        return None
    base = f"cauce resume {task_id}"
    if stop.cause == "permission" and stop.denied:
        return base + "".join(f" --allow {shlex.quote(rule)}" for rule in stop.denied)
    if stop.cause == "approval":
        return f"{base} --allow-approval"
    if stop.cause == "budget" and stop.budget_usd:
        return f"{base} --budget {stop.budget_usd * 2:g}"
    return base


def what_to_do(stop: Stop) -> str:
    """What a person does before the command, in one sentence."""
    return {
        "permission": "allow what was refused (or run it yourself), then resume",
        "environment": "fix what failed to run, then resume",
        "budget": "resume with a larger budget, or read the attempts first",
        "missing_dir": "the task's directory is gone: restore it, or queue the task again where it now lives",
        "approval": "approve the next cell by resuming with --allow-approval",
        "spec": "the task as written cannot be done: rewrite or split it as a new task",
        "turns": "the task is too big for one worker: split it into smaller tasks",
        "ladder": "no stronger model is left: rewrite the task, or read the attempts for what kept failing",
        "exhausted": "every cell failed: read the attempts, then resume or rewrite it",
        "attempts": "the attempt limit was reached: read the attempts, then resume",
        "cancelled": "resume it if you still want it",
        "signal": "resume it if you still want it",
        "died": "its process went away mid-run: resume it",
        "crashed": "cauce itself failed: the reason has the error; resume once it is fixed",
    }.get(stop.cause, "read the attempts")


def recovered(status: str, *, result: str = "", failure: str | None = None, move_reason: str = "",
              denied: tuple[str, ...] = (), next_cell: str | None = None, finished_reason: str = "",
              attempt: int | None = None) -> Stop | None:
    """The best account an older task's records give, marked `recovered`."""
    def made(cause: str, reason: str, **kw: Any) -> Stop:
        return Stop(cause, reason or status, recovered=True, attempt=attempt, **kw)

    reason = move_reason or result
    if status == "blocked":
        if failure == "permission" or denied:
            return made("permission", reason, denied=denied)
        if "budget" in result and result.startswith("the $"):
            return made("budget", result)
        if "directory no longer exists" in result:
            return made("missing_dir", result)
        return made("environment", reason)
    if status == "needs_approval":
        return made("approval", reason, next_cell=next_cell)
    if status == "replan":
        if failure in ("spec_bug", "architecture_bug"):
            return made("spec", reason)
        return made("turns" if failure == "turns_exhausted" else "ladder", reason)
    if status == "failed":
        return made("attempts" if "attempts without a pass" in result else "exhausted", reason)
    if status == "cancelled":
        return made("cancelled", result or "cancelled", by="unknown")
    if status == "interrupted":
        return made("died", finished_reason or result or "it stopped with no sign of life")
    return None


def record(store: Any, task_id: int, stop: Stop, *, result: bool = True, **fields: Any) -> None:
    """End a task that stopped outside a run's own loop (a cancel before it
    started, the dispatcher, the sweep): its status, its result and the account."""
    store.update_task(task_id, status=stop.status, **({"result": stop.reason} if result else {}), **fields)
    store.add_event(task_id, "finished", status=stop.status, reason=stop.reason, stop=stop.data())


def of(store: Any, task: dict[str, Any]) -> Stop | None:
    """The account of a task that stopped: the one recorded with its ending, else
    the one its records give. None for a task that is queued, running or done."""
    status = task["status"]
    if status in ("queued", "running", "done"):
        return None
    finished = store.last_event(task["id"], "finished")
    data = (finished or {}).get("data") or {}
    found = read(data.get("stop"))
    if found is not None and found.status == status:
        return found
    attempts = store.attempts(task["id"])
    last = attempts[-1] if attempts else {}
    moved = store.last_event(task["id"], "moved")
    moved_data = (moved or {}).get("data") or {}
    seq = last.get("seq")
    return recovered(
        status, result=str(task.get("result") or ""), failure=last.get("failure"),
        move_reason=str(last.get("move_reason") or ""),
        denied=tuple(store.denials(task["id"]).get(seq, ())) if seq is not None else (),
        next_cell=moved_data.get("next_cell"), finished_reason=str(data.get("reason") or ""), attempt=seq)


def view(store: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    """The account as the UI and `--json` show it: who in words, what to do, and
    the command, when there is one."""
    stop = of(store, task)
    if stop is None:
        return None
    resumable = task.get("source") == "cauce" and bool(task.get("cwd"))
    todo = what_to_do(stop) if resumable or stop.status != "cancelled" else (
        "it never ran: queue it again if you still want it")
    return {**stop.data(), "status": stop.status, "who": WHO.get(stop.who, stop.who), "todo": todo,
            "next": next_step(task["id"], stop, resumable=resumable)}
