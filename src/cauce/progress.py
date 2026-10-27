"""What a running task shows of itself while it runs: how long it has run against how long its
kind usually takes here, where it is (its worker, cauce's check, the independent check), what its
worker has written so far, and whether its worker has gone quiet.

A person who sent work to a worker sees it move, instead of trusting that it will end. All of it
is read: the store's events, the tool-call log's tail and its worktree's git; nothing is written,
no model is asked, and nothing is stopped. A view (the status line, the board, `cauce tasks`) may
call it on every refresh.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from cauce import isolate, phase
from cauce.config import home

#: A worker with no tool call for this long is said to be quiet: a long command, or stuck.
IDLE_S = 5 * 60
#: The passes of a kind (and of its size, when there are as many) an estimate needs.
ESTIMATE_SAMPLES = 3
#: The most new files whose lines are counted, and the largest one read.
NEW_FILES = 200
NEW_FILE_BYTES = 1_000_000
#: What each stage of a running attempt is called, as its `stage` event names it.
STAGES = {"worker": "worker", "check": "cauce's check", "verifier": "independent check"}


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def estimate(store: Any, task: Mapping[str, Any]) -> dict[str, Any] | None:
    """How long tasks like this one took to pass here: the median and the 90th percentile of the
    wall time of its kind's passes in its repository, of its size when enough of them were, else
    of its kind at any size. None with fewer than `ESTIMATE_SAMPLES` passes: no guess."""
    if not task.get("kind"):
        return None
    passed = [(t.get("points"), float(w)) for t in store.list_tasks(
                  repo=task.get("repo"), status=["done"], limit=200, source=("cauce", "queue"))
              if t["kind"] == task["kind"] and t["id"] != task.get("id")
              and (w := (((store.final_event(t["id"]) or {}).get("data")) or {}).get("wall_s"))]
    same = sorted(w for p, w in passed if task.get("points") and p == task.get("points"))
    walls, by = (same, "its kind and size") if len(same) >= ESTIMATE_SAMPLES else \
        (sorted(w for _, w in passed), "its kind")
    if len(walls) < ESTIMATE_SAMPLES:
        return None
    return {"p50_s": round(_quantile(walls, 0.5)), "p90_s": round(_quantile(walls, 0.9)), "samples": len(walls),
            "by": by}


def _git(path: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _artifact(path: str) -> bool:
    return any(part in isolate.ARTIFACT_DIRS for part in Path(path).parts)


def changes(task_id: int, base: str | None, root: Path | None = None) -> dict[str, Any] | None:
    """What a task's worker has written in its worktree so far, against the commit it started on:
    the files and the lines added and removed, new files counted whole. None when it works in no
    worktree, or git cannot say."""
    path = (root or home()) / "worktrees" / f"task-{task_id}"
    if not path.is_dir():
        return None
    numstat = _git(path, "diff", "--numstat", base or "HEAD", "--")
    untracked = _git(path, "ls-files", "--others", "--exclude-standard")
    if numstat is None or untracked is None:
        return None
    files, added, removed = set(), 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and not _artifact(parts[2]):
            files.add(parts[2])
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    new = [p for p in untracked.splitlines() if p and not _artifact(p)]
    for name in new[:NEW_FILES]:
        try:
            with (path / name).open("rb") as fh:
                added += fh.read(NEW_FILE_BYTES).count(b"\n")
        except OSError:
            continue
    files.update(new)
    return {"files": len(files), "added": added, "removed": removed}


def of(store: Any, task: Mapping[str, Any], *, now: float | None = None, root: Path | None = None,
       env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A running task's progress: elapsed time, stage, its worker's phase and how long since its
    last tool call, the estimate, and what it changed so far. What could not be read is left out."""
    now = time.time() if now is None else now
    task_id = int(task["id"])
    out: dict[str, Any] = {}
    planned = store.last_event(task_id, "planned")
    if planned and (began := _ts(planned["ts"])) is not None:
        out["elapsed_s"] = round(now - began)
    started = store.last_event(task_id, "attempt_started")
    seq = (started["data"] or {}).get("seq") if started else None
    stage = store.last_event(task_id, "stage")
    out["stage"] = STAGES.get(str((stage["data"] or {}).get("stage")), "worker") \
        if stage and started and stage["id"] > started["id"] else "worker"
    if (doing := phase.of(task_id, seq, env)) is not None:
        out["phase"] = doing
    if out["stage"] == "worker" and started:
        calls = phase._calls(task_id, seq, env)
        last = _ts(calls[-1].get("ts")) if calls else _ts(started["ts"])
        if last is not None:
            out["quiet_s"] = round(now - last)
    if (expected := estimate(store, task)) is not None:
        out["estimate"] = expected
    if (written := changes(task_id, ((started or {}).get("data") or {}).get("base"), root)) is not None:
        out["changes"] = written
    return out


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))}m" if seconds >= 60 else f"{round(seconds)}s"


def line(p: Mapping[str, Any]) -> str:
    """One running task's progress in one line, as the status line, `cauce tasks` and the mod's
    `tasks` tool say it: `implementing · 4m of ≈7m · 3 files +120 −8`."""
    parts = []
    doing = p.get("phase") if p.get("stage", "worker") == "worker" else p.get("stage")
    if doing:
        parts.append(str(doing))
    if (elapsed := p.get("elapsed_s")) is not None:
        expected = p.get("estimate")
        parts.append(f"{_minutes(elapsed)} of ≈{_minutes(expected['p50_s'])}" if expected else _minutes(elapsed))
    if written := p.get("changes"):
        if written["files"]:
            parts.append(f"{written['files']} file{'s' if written['files'] != 1 else ''} "
                         f"+{written['added']} −{written['removed']}")
        else:
            parts.append("nothing written yet")
    if (expected := p.get("estimate")) and (elapsed := p.get("elapsed_s")) is not None \
            and elapsed > expected["p90_s"]:
        parts.append(f"past 9 in 10 of its kind here ({_minutes(expected['p90_s'])})")
    if (quiet := p.get("quiet_s")) is not None and quiet >= IDLE_S:
        parts.append(f"no tool call for {_minutes(quiet)} (a long command, or stuck)")
    return " · ".join(parts)


def expected_line(store: Any, task: Mapping[str, Any]) -> str:
    """What sending a task says of how long it should take: its kind's usual time here. A task
    not classified yet is read by the rules alone (no model); one they cannot read gets none."""
    if not task.get("kind"):
        from cauce.classify import by_rules

        try:
            asked = json.loads(task.get("options") or "{}").get("kind")
        except (ValueError, AttributeError):
            asked = None
        ruled = None if asked else by_rules(str(task.get("body") or task.get("title") or ""))
        task = {**task, "kind": asked or (ruled.kind if ruled else None)}
    found = estimate(store, task)
    if found is None:
        return ""
    return (f"tasks like it took ≈{_minutes(found['p50_s'])} here (9 in 10 within {_minutes(found['p90_s'])}; "
            f"{found['samples']} passes of {found['by']})")
