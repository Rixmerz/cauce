"""Records written in two steps that another process reads between them: each one is one write."""
from __future__ import annotations

import os

from locks import flow
from locks.store import Store


def test_a_run_is_never_running_without_its_process(git_repo, store: Store, monkeypatch):
    """A run marked running and given its pid in two writes was read in between by a sweep as dead (a
    running task with no pid): a resume that had just started was stopped with no crash named, and when
    it died for real nobody saw it (the all-on CI run of the repeated-crash scenario stuck on this)."""
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    seen: list[tuple[str, object]] = []
    update, create = store.update_task, store.create_task

    def watched_update(task_id, **fields):
        update(task_id, **fields)
        task = store.get_task(task_id)
        seen.append((task["status"], task["pid"]))

    def watched_create(*args, **fields):
        task = create(*args, **fields)
        seen.append((task["status"], task["pid"]))
        return task

    monkeypatch.setattr(store, "update_task", watched_update)
    monkeypatch.setattr(store, "create_task", watched_create)
    first = run("Rename the helper in notes", git_repo, store, registry={}, launcher=Script(ok()),
                classifier=kind("implement"))
    queued = create("Rename the other helper", status="queued", source="queue", repo="r", cwd=str(git_repo))
    run("Rename the other helper", git_repo, store, registry={}, launcher=Script(ok()),
        classifier=kind("implement"), task_id=queued["id"])
    assert first.status == "done"
    running = [pid for status, pid in seen if status == "running"]
    assert running and all(pid == os.getpid() for pid in running), seen
    # And the sweep reads a running task with a live pid as alive.
    assert flow.sweep(store) == []


def test_the_doctor_names_a_run_swept_before_its_pid(store: Store):
    from locks import doctor

    from .test_doctor import LATER, rules

    def swept(version: str, reason: str = "no sign of life") -> int:
        task = store.create_task("work", status="interrupted", source="locks", repo="r")["id"]
        store.add_event(task, "finished", status="interrupted", version=version,
                        stop={"cause": "died", "by": "locks", "reason": reason})
        return task

    now = swept("0.66.0")
    swept("0.66.0", "no sign of life: its process (123) is gone")
    old = swept("0.65.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["swept-before-its-pid"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, now), (doctor.FIXED, old)]
