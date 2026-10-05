"""Claude Code hooks: every prompt becomes a task, and what is said about it stays with it.

- **UserPromptSubmit** — a new prompt is a new task. A message typed while a
  turn is running belongs to that turn's task, not to one of its own. A bare
  "continue" folds into the turn it pushes along. When the prompt matches a
  fix that already failed — here or in another repository — the dead end is
  put in front of the model before it starts.
- **Stop** — the turn's final message is the task's result. Any other task
  still running in the session belongs to a turn the user interrupted; Claude
  Code fires no Stop for those, so they are marked interrupted now.
- **StopFailure** — the turn died; its task failed.
- **SessionStart** — on resume or compaction, the unfinished tasks of the
  session; always, the recent dead ends of this repository.

Hooks fail open: an exception never reaches Claude Code, because a hook that
raises takes the user's session down. But failing open is not failing silently
— the error goes to a ledger, and the next SessionStart says it happened.
"""
from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from cauce import repo
from cauce.store import Store, home
from cauce.text import fold, words

_NUDGES = frozenset({
    "sigue", "continua", "continue", "--continue", "go on", "keep going", "dale", "sigue porfa",
    "sigue por favor", "continua por favor", "please continue", "proceed", "procede",
})
_MAX_DEAD_ENDS = 3
ERROR_LOG = "hook-errors.log"


def _context(event_name: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def _ignored(text: str) -> bool:
    stripped = text.strip()
    # Harness-generated turns (task notifications, local command output) are not
    # requests anyone made.
    return not stripped or stripped.startswith(("<task-notification", "<command-", "<local-command", "<system-"))


def _dead_end_text(dead: list[dict], intro: str) -> str:
    lines = [intro]
    for d in dead:
        line = f"- {d['problem']} ({d['repo'] or 'unknown repo'}): tried {d['tried'][:200]}"
        if d["why"]:
            line += f"; failed: {d['why'][:120]}"
        if d["worked_instead"]:
            line += f"; what worked: {d['worked_instead'][:200]}"
        lines.append(line)
    return "\n".join(lines)


def user_prompt_submit(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    text = str(event.get("prompt") or "")
    if not session_id or _ignored(text):
        return None
    cwd = event.get("cwd")
    key = repo.key(cwd) if cwd else None
    prompt_id = event.get("prompt_id")
    store.touch_session(session_id, cwd, key)

    running = store.running_task(session_id, prompt_id) if prompt_id else None
    if running is not None:
        store.add_message(running["id"], "user", text)
        return None

    if " ".join(fold(text).split()) in _NUDGES:
        previous = store.last_prompt_task(session_id)
        if previous is not None and previous["status"] in ("interrupted", "failed"):
            store.add_message(previous["id"], "user", text)
            store.update_task(previous["id"], status="running", prompt_id=prompt_id)
            return None

    store.create_task(text, status="running", source="hook", session_id=session_id, prompt_id=prompt_id,
                      cwd=cwd, repo=key)
    if len(words(text)) < 3:
        return None
    dead = store.dead_ends(text, repo=key, limit=_MAX_DEAD_ENDS, strict=True)
    if not dead:
        return None
    return _context("UserPromptSubmit", _dead_end_text(
        dead, "cauce memory: fixes already tried against problems like this one, and they did not work. "
        "Before applying one again, say so and check why it failed."))


def stop(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    prompt_id = event.get("prompt_id")
    target = store.running_task(session_id, prompt_id) if prompt_id else store.running_task(session_id)
    message = str(event.get("last_assistant_message") or "")
    if target is not None:
        store.update_task(target["id"], status="done", result=message[:20000])
        if message:
            store.add_message(target["id"], "assistant", message)
    store.interrupt_running(session_id, exclude=[target["id"]] if target else [])
    return None


def stop_failure(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    target = store.running_task(session_id, event.get("prompt_id")) or store.running_task(session_id)
    if target is not None:
        reason = str(event.get("error") or event.get("reason") or "the turn failed")
        store.update_task(target["id"], status="failed", result=reason[:2000])
    return None


def session_start(event: Mapping[str, Any], store: Store, root: Path) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    cwd = event.get("cwd")
    key = repo.key(cwd) if cwd else None
    store.touch_session(session_id, cwd, key)
    blocks: list[str] = []
    errors = _drain_errors(root)
    if errors:
        blocks.append(f"cauce: {errors} hook error(s) since the last session were swallowed so the session "
                      f"could go on; details in {root / ERROR_LOG}.")
    if event.get("source") in ("resume", "compact"):
        unfinished = store.list_tasks(session_id=session_id, status=("running", "interrupted"), limit=10)
        if unfinished:
            blocks.append("cauce: unfinished tasks in this session (context only, do not restart them unasked):\n"
                          + "\n".join(f"#{t['id']} [{t['status']}] {t['title']}" for t in unfinished))
    if key:
        dead = store.dead_ends(repo=key, limit=5)
        if dead:
            blocks.append(_dead_end_text(dead, "cauce memory: recent fixes in this repository that did not work."))
    return _context("SessionStart", "\n\n".join(blocks)) if blocks else None


HANDLERS = {
    "UserPromptSubmit": user_prompt_submit,
    "Stop": stop,
    "StopFailure": stop_failure,
}


def handle(event_name: str, event: Mapping[str, Any], store: Store, root: Path) -> dict | None:
    if event_name == "SessionStart":
        return session_start(event, store, root)
    handler = HANDLERS.get(event_name)
    return handler(event, store) if handler else None


def main(event_name: str, stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    """Always exits 0 and never writes anything but a hook answer to stdout."""
    if env.get("CAUCE_HOOKS_OFF") == "1":
        return 0
    root = home(dict(env))
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
        store = Store(root / "cauce.db")
        try:
            answer = handle(event_name, event, store, root)
        finally:
            store.close()
        if answer:
            stdout.write(json.dumps(answer))
    except Exception:  # the contract: a hook never takes the session down
        _note_error(root, event_name)
    return 0


def _note_error(root: Path, event_name: str) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (root / ERROR_LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp} {event_name}\n{traceback.format_exc()}\n")
    except Exception:  # noqa: S110 - losing a note is acceptable, raising from here is not
        pass


def _drain_errors(root: Path) -> int:
    """How many errors were noted since the last drain. The log is kept for
    reading; a marker file remembers how much of it was already reported."""
    log, mark = root / ERROR_LOG, root / (ERROR_LOG + ".reported")
    try:
        if not log.is_file():
            return 0
        total = log.read_text(encoding="utf-8").count("\n--- ") + (
            1 if log.read_text(encoding="utf-8").startswith("--- ") else 0)
        seen = int(mark.read_text(encoding="utf-8")) if mark.is_file() else 0
        mark.write_text(str(total), encoding="utf-8")
        return max(total - seen, 0)
    except (OSError, ValueError):
        return 0
