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
  session; always, the recent dead ends of this repository. When `cauce` does
  not resolve on PATH, it is put there for the session's commands.

Hooks fail open: an exception never reaches Claude Code, because a hook that
raises takes the user's session down. But failing open is not failing silently
— the error goes to a ledger, and the next SessionStart says it happened.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from cauce import config, repo
from cauce.store import Store, home
from cauce.text import fold, words

_NUDGES = frozenset({
    "sigue", "continua", "continue", "--continue", "go on", "keep going", "dale", "sigue porfa",
    "sigue por favor", "continua por favor", "please continue", "proceed", "procede",
})
_MAX_DEAD_ENDS = 3
QUEUE_PREFIX = "++"
DELEGATION_TOOLS = frozenset({"Agent", "Task"})


def _queue_request(text: str) -> str | None:
    """The task text after the queue prefix, "" for a bare prefix, None otherwise."""
    stripped = text.lstrip()
    if not stripped.startswith(QUEUE_PREFIX):
        return None
    return stripped[len(QUEUE_PREFIX):].strip()
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
    if session_id and text.lstrip().startswith("<task-notification"):
        _background_child_finished(session_id, text, store)
        return None
    if not session_id or _ignored(text):
        return None
    cwd = event.get("cwd")
    key = repo.key(cwd) if cwd else None
    prompt_id = event.get("prompt_id")
    store.touch_session(session_id, cwd, key)

    queued = _queue_request(text)
    if queued is not None:
        if not queued:
            return {"decision": "block", "reason": f'cauce: "{QUEUE_PREFIX} <task>" queues a task for a worker'}
        task = store.enqueue(queued, repo=key, cwd=cwd, session_id=session_id)
        if cwd:
            _enroll(cwd)
        # Blocked: queuing costs no turn. The reason is what the person sees.
        return {"decision": "block", "reason": f"cauce: queued #{task['id']} — {task['title']}. "
                + _dispatch(cwd, key, os.environ)}

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


def _dispatch(cwd: str | None, key: str | None, env: Mapping[str, str]) -> str:
    """Start the repository's dispatcher (the `autowork` setting), and say what happens next."""
    if not cwd or not config.enabled("autowork", env):
        return "`cauce work` runs the queue in workers."
    from cauce import dispatch

    root = home(dict(env))
    if dispatch.held(root, key or "*") or dispatch.start(Path(cwd), root, key or "*"):
        if config.enabled("parallel", env):
            return "A worker takes it now, beside the running ones if Haiku finds it independent, or after them."
        return "A worker takes it when the tasks ahead of it are done."
    return "Starting a worker failed; `cauce work` runs the queue."


def stop(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    prompt_id = event.get("prompt_id")
    target = store.running_task(session_id, prompt_id) if prompt_id else store.running_task(session_id)
    message = str(event.get("last_assistant_message") or "")
    if target is not None:
        # A subagent this turn started in the background is still working: the
        # turn's answer is in, but the task is not done until its children are.
        waiting = store.children(target["id"], status=["running"])
        store.update_task(target["id"], status="running" if waiting else "done", result=message[:20000])
        if message:
            store.add_message(target["id"], "assistant", message)
    store.interrupt_running(session_id, exclude=[target["id"]] if target else [])
    if event.get("transcript_path"):
        from cauce import usage

        # What this session's turns cost, by the model that served them.
        usage.ingest(store, event["transcript_path"], session_id=session_id,
                     task_id=target["id"] if target else None, repo=target["repo"] if target else None)
    from cauce import naming

    naming.maybe_start(store, session_id, home(), os.environ)
    return None


def _enroll(cwd: str) -> None:
    """Using cauce in a project enrolls it: it gets its `.cauce/` folder."""
    from cauce import project

    with contextlib.suppress(OSError):
        project.enroll(cwd)


def pre_tool_use(event: Mapping[str, Any], store: Store) -> dict | None:
    """A delegation to a subagent becomes a child of the turn that made it."""
    if event.get("tool_name") not in DELEGATION_TOOLS or not event.get("session_id"):
        return None
    parent = store.running_task(event["session_id"])
    tool_input = event.get("tool_input") or {}
    body = str(tool_input.get("prompt") or tool_input.get("description") or "delegated work")
    store.create_task(body, status="running", source="delegation", session_id=event["session_id"],
                      parent_id=parent["id"] if parent else None, cwd=event.get("cwd"),
                      title=str(tool_input.get("description") or "") or None,
                      repo=parent["repo"] if parent else None, prompt_id=event.get("tool_use_id"))
    return None


def post_tool_use(event: Mapping[str, Any], store: Store) -> dict | None:
    """A delegation that returned closes its child task, and the turn too when it
    was the last child. (Every tool call's signature is logged before this, by the
    fast path in `cauce.__main__`.)"""
    if not event.get("session_id") or event.get("tool_name") not in DELEGATION_TOOLS:
        return None
    child = store.delegation(event["session_id"], event.get("tool_use_id"))
    if child is None:
        return None
    if (event.get("tool_input") or {}).get("run_in_background"):
        # The call returned at launch; the work ends when its notification arrives.
        return None
    response = event.get("tool_response")
    text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)[:20000]
    _finish_child(store, child, "done", text)
    return None


def _finish_child(store: Store, child: dict, status: str, result: str) -> None:
    store.update_task(child["id"], status=status, result=result)
    parent = store.get_task(child["parent_id"]) if child["parent_id"] else None
    if (parent and parent["status"] == "running" and parent["result"]
            and not store.children(parent["id"], status=["running"])):
        store.update_task(parent["id"], status="done")


_NOTE_TAG = re.compile(r"<(tool-use-id|status|summary|result)>(.*?)</\1>", re.DOTALL)


def _background_child_finished(session_id: str, text: str, store: Store) -> None:
    fields = {k: v.strip() for k, v in _NOTE_TAG.findall(text)}
    child = store.delegation(session_id, fields.get("tool-use-id")) if fields.get("tool-use-id") else None
    if child is None or child["status"] != "running":
        return
    status = "done" if fields.get("status", "completed") == "completed" else "failed"
    _finish_child(store, child, status, fields.get("result") or fields.get("summary") or "")


def stop_failure(event: Mapping[str, Any], store: Store) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    target = store.running_task(session_id, event.get("prompt_id")) or store.running_task(session_id)
    if target is not None:
        reason = str(event.get("error") or event.get("reason") or "the turn failed")
        store.update_task(target["id"], status="failed", result=reason[:2000])
    return None


def session_start(
    event: Mapping[str, Any],
    store: Store,
    root: Path,
    env: Mapping[str, str] | None = None,
    adapters: list | None = None,
) -> dict | None:
    session_id = event.get("session_id")
    if not session_id:
        return None
    env = env or {}
    config.sync_plugin_options(env)
    from cauce import flow  # imported here: it pulls in the orchestrator

    flow.sweep(store)
    cwd = event.get("cwd")
    key = repo.key(cwd) if cwd else None
    store.touch_session(session_id, cwd, key)
    blocks: list[str] = []
    _export(env, "CAUCE_SESSION_ID", str(session_id))
    reach = _reach(env)
    if reach:
        blocks.append(reach)
    errors = _drain_errors(root)
    if errors:
        blocks.append(f"cauce: {errors} hook error(s) since the last session were swallowed so the session "
                      f"could go on; details in {root / ERROR_LOG}.")
    if event.get("source") in ("resume", "compact"):
        unfinished = store.list_tasks(session_id=session_id, status=("running", "interrupted"), limit=10)
        if unfinished:
            blocks.append("cauce: unfinished tasks in this session (context only, do not restart them unasked):\n"
                          + "\n".join(f"#{t['id']} [{t['status']}] {t['title']}" for t in unfinished))
    if cwd and repo.toplevel(Path(cwd)) is not None:
        blocks += _freshen(Path(cwd), root, env, adapters)
    if key:
        dead = store.dead_ends(repo=key, limit=5)
        if dead:
            blocks.append(_dead_end_text(dead, "cauce memory: recent fixes in this repository that did not work."))
    return _context("SessionStart", "\n\n".join(blocks)) if blocks else None


def _export(env: Mapping[str, str], name: str, value: str) -> bool:
    """Set a variable for the session's Bash commands, through `CLAUDE_ENV_FILE`.
    `CAUCE_SESSION_ID` is how a `cauce run` or `cauce queue add` typed in a
    session is recorded as that session's."""
    env_file = env.get("CLAUDE_ENV_FILE")
    if not env_file:
        return False
    try:
        with open(env_file, "a", encoding="utf-8") as out:
            out.write(f"export {name}={shlex.quote(value)}\n")
        return True
    except OSError:
        return False


def _reach(env: Mapping[str, str]) -> str | None:
    """Make `cauce` resolve in the session's Bash commands.

    The commands call it by name. Claude Code puts a plugin's `bin/` on the Bash
    tool's PATH, but older versions do not, and neither does a session opened
    before the plugin was installed. A SessionStart hook can extend the session's
    environment through `CLAUDE_ENV_FILE`; without one, the model is told the full
    path instead of finding out from an exit 127.
    """
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return None
    bin_dir = Path(plugin_root) / "bin"
    if not (bin_dir / "cauce").is_file() or shutil.which("cauce", path=env.get("PATH", "")):
        return None
    env_file = env.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as out:
                out.write(f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"\n')
            return None
        except OSError:
            pass
    return (f"cauce: the `cauce` command is not on PATH in this session; "
            f"run it as {shlex.quote(str(bin_dir / 'cauce'))}.")


def _freshen(cwd: Path, root: Path, env: Mapping[str, str], adapters: list | None) -> list[str]:
    """Start a background index for an adopted neighbour whose index is missing
    or older than the last commit, so it is current by the time work starts."""
    from cauce.adapters import ABSENT, default_adapters

    if adapters is None:
        adapters = default_adapters() if config.enabled("livespec", env) else []
    notes = []
    for adapter in adapters:
        status = adapter.inspect(cwd)
        missing = status.state == ABSENT and adapter.server() is not None
        if (missing or status.stale) and adapter.refresh_in_background(cwd, root / f"{adapter.name}-index.log"):
            notes.append(f"cauce: {'indexing' if missing else 'refreshing'} this repository with "
                         f"{adapter.name} in the background.")
    return notes


HANDLERS = {
    "UserPromptSubmit": user_prompt_submit,
    "PreToolUse": pre_tool_use,
    "PostToolUse": post_tool_use,
    "Stop": stop,
    "StopFailure": stop_failure,
}


def handle(
    event_name: str, event: Mapping[str, Any], store: Store, root: Path, env: Mapping[str, str] | None = None
) -> dict | None:
    if event_name == "SessionStart":
        return session_start(event, store, root, env)
    handler = HANDLERS.get(event_name)
    return handler(event, store) if handler else None


def main(event_name: str, stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    """Always exits 0 and never writes anything but a hook answer to stdout."""
    root = home(dict(env))
    if env.get("CAUCE_HOOKS_OFF") == "1":
        return 0
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
        store = Store(root / "cauce.db")
        try:
            answer = handle(event_name, event, store, root, env)
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
