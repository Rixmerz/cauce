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
#: How much of the log's tail is read, and how many of the attempt's last calls weigh.
TAIL_BYTES = 256 * 1024
LAST = 6
_CHECKS = ("test", "pytest", "jest", "vitest", "mocha", "tsc", "lint", "ruff", "mypy", "eslint", "build", "check",
           "cargo:test", "go:test", "go:vet", "clippy", "coverage", "rspec", "phpunit")


def kind_of(sig: str) -> str:
    if sig.startswith(("edit", "write")):
        return "implementing"
    if sig.startswith("bash") and any(word in sig for word in _CHECKS):
        return "checking"
    return "exploring"


def recent(task_id: int, attempt: int | None, env: Mapping[str, str] | None = None) -> list[str]:
    """The signatures of the attempt's last calls, oldest first."""
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
        if e.get("task_id") == task_id and (attempt is None or e.get("attempt") == attempt):
            out.append(str(e.get("sig") or ""))
    return out[-LAST:]


def of(task_id: int, attempt: int | None, env: Mapping[str, str] | None = None) -> str | None:
    """The attempt's phase now, or None before its first call."""
    calls = recent(task_id, attempt, env)
    if not calls:
        return None
    kinds = [kind_of(s) for s in calls]
    if "checking" in kinds[-3:]:
        return "checking"
    if "implementing" in kinds:
        return "implementing"
    return "exploring"
