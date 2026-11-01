"""What a running attempt is doing now: exploring, implementing or checking,
read from the last tool calls its worker made.

The calls are read from the append-only log the PostToolUse fast path writes,
the tail of today's file only, and nothing is written: the UI's GETs and the
mod's status line call this. No model is asked.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime

from cauce import signature

PHASES = ("exploring", "implementing", "checking")
#: How much of the log's tail is read.
TAIL_BYTES = 256 * 1024
_CHECKS = ("test", "pytest", "jest", "vitest", "mocha", "tsc", "lint", "ruff", "mypy", "eslint", "build", "check",
           "cargo:test", "go:test", "go:vet", "clippy", "coverage", "rspec", "phpunit")


def kind_of(sig: str) -> str:
    if sig.startswith(("edit", "write")):
        return "implementing"
    if sig.startswith("bash") and any(word in sig for word in _CHECKS):
        return "checking"
    return "exploring"


def _calls(task_id: int, attempt: int | None, env: Mapping[str, str] | None) -> list[dict]:
    """The attempt's calls in the tail of today's log, oldest first."""
    path = signature.log_dir(dict(os.environ) if env is None else env) / f"{datetime.now(UTC):%Y-%m-%d}.ndjson"
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("task_id") == task_id and (attempt is None or e.get("attempt") == attempt):
            out.append(e)
    return out


#: How many of an attempt's last calls the task page shows.
SHOWN = 8


def calls(task_id: int, attempt: int | None, env: Mapping[str, str] | None = None, *,
          limit: int = SHOWN) -> list[dict]:
    """The attempt's last calls as the task page shows them, oldest first: when, the tool, its
    signature, whether it went through, and the phase it counts toward. Only what a signature
    keeps: never an argument, nor the hash of one."""
    out = []
    for e in _calls(task_id, attempt, env)[-limit:]:
        sig = str(e.get("sig") or "")
        out.append({"ts": e.get("ts"), "tool": str(e.get("tool") or ""), "sig": sig, "ok": bool(e.get("ok", 1)),
                    "kind": kind_of(sig)})
    return out


def session_of(task_id: int, attempt: int | None, env: Mapping[str, str] | None = None) -> str | None:
    """The Claude Code session the attempt's worker runs in, as its last call said; None before
    its first call, or when the log's tail no longer holds one."""
    for e in reversed(_calls(task_id, attempt, env)):
        if isinstance(e.get("session_id"), str) and e["session_id"]:
            return e["session_id"]
    return None


def of(task_id: int, attempt: int | None, env: Mapping[str, str] | None = None) -> str | None:
    """The attempt's phase now, or None before its first call. A write anywhere in the attempt
    makes it implementing, not only one of its last calls: a worker reads again after it writes."""
    sigs = [str(e.get("sig") or "") for e in _calls(task_id, attempt, env)]
    if not sigs:
        return None
    if "checking" in [kind_of(s) for s in sigs[-3:]]:
        return "checking"
    if any(kind_of(s) == "implementing" for s in sigs):
        return "implementing"
    return "exploring"
