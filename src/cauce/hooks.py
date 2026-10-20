"""Claude Code hooks: every prompt becomes a task, and what is said about it stays with it.

- **UserPromptSubmit** — a new prompt is a new task. A message typed while a
  turn is running belongs to that turn's task, not to one of its own. A bare
  "continue" folds into the turn it pushes along. When the prompt matches a
  fix that already failed — here or in another repository — the dead end is
  put in front of the model before it starts; when it matches this project's
  notes, so are they.
- **Stop** — the turn's final message is the task's result. Any other task
  still running in the session belongs to a turn the user interrupted; Claude
  Code fires no Stop for those, so they are marked interrupted now. The scribe
  reads what the transcript gained (`cauce.scribe`) and, every few turns, has
  the project's notes filed.
- **StopFailure** — the turn died; its task failed.
- **SessionStart** — on resume or compaction, the unfinished tasks of the
  session; on a new session where a person left a relay (`cauce relay`), the
  thread of the session it continues; always, the recent dead ends of this
  repository and the index of its notes. When `cauce` does not resolve on PATH, it is put there for the
  session's commands.
- **PreCompact**, **SessionEnd** — a detached Haiku reads what the session
  said since the last time and keeps its durable facts in the project's notes,
  before the conversation is summarized away or closed.
- **Stop**, **PreCompact**, **PostCompact** — whether a turn that ended is a
  good moment to compact, and every compaction seen, for the mod to suggest or
  submit `/compact` and learn whether it ran (`cauce.compaction`).

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
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from cauce import config, orchestrator, project, repo, stops
from cauce import findings as findings_mod
from cauce.notes import _SECRET
from cauce.store import OPEN_FOR_A_RELAY, Store, home
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


#: How Claude Code hands a background subagent's final report back to the session.
_HANDBACK = re.compile(r'^<agent-message from="([^"]+)">\s*\[Subagent hand-back\]')


#: How the endings notice opens: the mod submits it as a prompt to wake an idle session.
ENDINGS_HEADER = "cauce: work this session sent has ended since you last heard."
#: A prompt a plugin submitted in the person's place, as Claude Code words it.
_PLUGIN_SENT = re.compile(r"^The [\w.:@-]+ plugin sent a message")


def _ignored(text: str) -> bool:
    from cauce.compact import ASKED

    stripped = text.strip()
    # Harness-generated turns (task notifications, local command output) are not
    # requests anyone made, and neither is a notice cauce itself put in the
    # session, nor a `/compact` the mod submitted that reached the model as a
    # message: none of them is a turn to record as a task.
    return (not stripped or stripped.startswith(("<task-notification", "<command-", "<local-command", "<system-",
                                                 ENDINGS_HEADER, ASKED))
            or bool(_PLUGIN_SENT.match(stripped)) or bool(_ECHO.fullmatch(stripped)))


#: A prompt that is only a command line's flags (`--continue`, `--resume <id>`) or a bare
#: slash command (`/clear`): an echo of how a session was started or cleared, no request.
_ECHO = re.compile(r"(?:--?[A-Za-z][\w-]*(?:[= ][^\s-]\S*)?\s*)+|/[A-Za-z][\w:-]*")


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
    if session_id and (handback := _HANDBACK.match(text.lstrip())):
        _handed_back(store, event, handback.group(1), text)
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
        task = store.enqueue(queued, repo=key, cwd=cwd, session_id=session_id, author="person")
        if cwd:
            _enroll(cwd)
            orchestrator.sent(store, cwd, {"CAUCE_SESSION_ID": str(session_id)})
        # Blocked: queuing costs no turn. The reason is what the person sees.
        return {"decision": "block", "reason": f"cauce: queued #{task['id']} — {task['title']}. "
                + _kick(cwd, key)}

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

    task = store.create_task(text, status="running", source="hook", session_id=session_id, prompt_id=prompt_id,
                             cwd=cwd, repo=key)
    blocks = [notice] if (notice := _endings(store, session_id, via="prompt")) else []
    # The session delegates: it hears only of failures met across areas, never one
    # cell's (`cauce.cells`), which goes to the workers of that cell.
    from cauce import cells

    dead = cells.dead_ends(store, text, repo=key, shared_only=True, limit=_MAX_DEAD_ENDS) \
        if len(words(text)) >= 3 else []
    if dead:
        store.add_event(task["id"], "dead_ends", shown=dead)
        blocks.append(_dead_end_text(
            dead, "cauce memory: fixes already tried against problems like this one, and they did not work. "
            "Before applying one again, say so and check why it failed."))
    if key and cwd and len(words(text)) >= 3 and config.enabled("notes", os.environ) and project.find(cwd):
        from cauce import notes

        known = notes.recall(store, key, text, limit=3, hops=0, strict=True)
        if known:
            store.add_event(task["id"], "notes_shown", ids=[n["id"] for n in known])
            blocks.append("cauce notes: what this project's notes say about this (kept from earlier work; check "
                          "them against the code, and `cauce recall \"<question>\"` for more):\n"
                          + "\n".join(f"- {notes.line(n, text_chars=300)}" for n in known))
    if routed := _route(store, task["id"], text, cwd, event.get("transcript_path")):
        blocks.append(routed)
    return _context("UserPromptSubmit", "\n\n".join(blocks)) if blocks else None


def _route(store: Store, task_id: int, text: str, cwd: str | None, transcript: str | None) -> str | None:
    """Which way the request is best done (`cauce.route`): said only when it is not the
    session itself, the default, and kept on the turn either way, to be measured. Only in
    an enrolled project, with the `route` setting on; it never costs the turn."""
    if not cwd or not config.enabled("route", os.environ) or project.find(cwd) is None:
        return None
    try:
        from cauce import compaction, route

        share, tokens = None, None
        if transcript:
            context = compaction.read_context(Path(transcript))
            tokens = context.tokens
            share = context.tokens / context.window if context.tokens else None
        footprint = store.worker_footprint(repo.key(cwd))
        chosen = route.choose(text, context_tokens=tokens, context_share=share, footprint=footprint)
        store.add_event(task_id, "routed", **chosen.data(), context_tokens=tokens,
                        worker_tokens=footprint[0] if footprint else None)
        return chosen.advice() or None
    except Exception:  # the turn goes on
        note_error(home(), "route")
        return None


def _handed_back(store: Store, event: Mapping[str, Any], agent_id: str, text: str) -> None:
    """A subagent's report is no request: it joins the task that delegated the work,
    which records the answer the session gives on it."""
    session_id = str(event["session_id"])
    tool_use = None
    if event.get("transcript_path"):
        from cauce import usage

        tool_use = usage.subagent_tool_use(event["transcript_path"], agent_id)
    child = store.delegation(session_id, tool_use) if tool_use else None
    task = store.get_task(child["parent_id"]) if child and child["parent_id"] else store.last_prompt_task(session_id)
    if task is None:
        return
    store.add_message(task["id"], "subagent", text)
    store.update_task(task["id"], status="running", prompt_id=event.get("prompt_id"))


def _kick(cwd: str | None, key: str | None) -> str:
    from cauce import dispatch

    return dispatch.kick(cwd, key, os.environ)


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
        counted = {"session_id": session_id, "task_id": target["id"] if target else None,
                   "repo": target["repo"] if target else None}
        usage.ingest(store, event["transcript_path"], **counted)
        usage.ingest_subagents(store, event["transcript_path"], **counted)
    _scribe(event, home(), os.environ)
    _evolve(store, home())
    cwd = event.get("cwd")
    _landed(store, repo.toplevel(Path(cwd)) if cwd and Path(cwd).is_dir() else None, home())
    _revive(store, home(), os.environ)
    from cauce import naming

    naming.maybe_start(store, session_id, home(), os.environ)
    _adopt(store, str(session_id), event.get("cwd"))
    notice = _endings(store, session_id, via="stop")
    if notice:
        # The turn would end without the session knowing its work stopped:
        # it goes on, once per ending, to tell the person and continue.
        return {"decision": "block", "reason": notice}
    _consider_compaction(event, store, target, home(), os.environ)
    return None


def _adopt(store: Store, session_id: str, cwd: str | None) -> int:
    """The orchestrator takes what sessions gone quiet left in its project, so their
    endings reach it with its own."""
    if not cwd:
        return 0
    try:
        if not orchestrator.is_it(project.find(cwd), session_id):
            return 0
        key = repo.key(cwd)
        return orchestrator.adopt(store, [key], session_id) if key else 0
    except OSError:
        return 0


def _consider_compaction(event: Mapping[str, Any], store: Store, target: dict | None, root: Path,
                         env: Mapping[str, str]) -> None:
    """The turn ended for good: is now the moment to compact (`cauce.compaction`)?
    The mod reads the decision; its failure is noted and costs the turn nothing."""
    turn = target or store.last_prompt_task(str(event["session_id"]))
    if not event.get("transcript_path") or turn is None:
        return
    try:
        from cauce import compaction

        compaction.consider(store, root, session_id=str(event["session_id"]),
                            transcript=Path(str(event["transcript_path"])), cwd=event.get("cwd"), task_id=turn["id"],
                            env=env)
    except Exception:  # the turn ends as it would have
        note_error(root, "Stop (compaction)")


def _compaction_seen(store: Store, root: Path, session_id: str, source: str, *, trigger: Any = None,
                     instructions: Any = None) -> None:
    """Keep a compaction the hooks saw (`cauce.compaction.seen`), after the digest
    is kept or put back: its failure is noted and costs the hook nothing else."""
    try:
        from cauce import compaction

        compaction.seen(store, session_id, source, trigger=trigger, instructions=instructions)
    except Exception:  # the digest and its restore never depend on this record
        note_error(root, f"{source} (compaction)")


def _scribe(event: Mapping[str, Any], root: Path, env: Mapping[str, str]) -> None:
    """The scribe keeps the session's facts as it goes (`cauce.scribe`), and every few
    turns has the project's notes filed: a session that never compacts still feeds
    them. Its own failure is noted, and never costs the turn its endings."""
    if not event.get("transcript_path") or not config.enabled("scribe", env):
        return
    try:
        from cauce import scribe

        state = scribe.update(root, str(event["session_id"]), str(event["transcript_path"]),
                              max_bytes=scribe.HOOK_BYTES)
        if state is not None and scribe.notes_due(state) and keep_notes(event, root, env):
            scribe.notes_started(root, state)
    except Exception:  # the turn's endings still go out
        note_error(root, "Stop (scribe)")


def _revive(store: Store, root: Path, env: Mapping[str, str]) -> None:
    """A worker that died is noticed and resumed (`dispatch.recover`), and a lane with queued
    work and no dispatcher (one died on a full disk, or was killed) gets one again
    (`dispatch.revive`). Its failure is noted, and never costs the turn."""
    try:
        from cauce import dispatch

        dispatch.recover(store, root, env)
        dispatch.revive(store, root, env)
    except Exception:  # the turn goes on
        note_error(root, "revive")


def _landed(store: Store, top: Path | None, root: Path) -> None:
    """Stopped tasks of this checkout whose kept work is already on its HEAD leave the
    board (`stops.landed`). Its failure is noted, and never costs the turn."""
    if top is None:
        return
    try:
        stops.landed(store, top)
    except Exception:  # the turn goes on
        note_error(root, "landed")


def _evolve(store: Store, root: Path) -> None:
    """What ended settles the standards and, on a new day, the evolution history
    (`cauce.evolution`). Its failure is noted, and never costs the turn its endings."""
    try:
        from cauce import evolution

        evolution.settle(store)
    except Exception:  # the turn's endings still go out
        note_error(root, "Stop (evolution)")


def _relay(store: Store, root: Path, session_id: str, cwd: str, transcript: str | None) -> str | None:
    """A session started where a person handed one over (`cauce relay`): it starts
    from that session's thread, and the work that session sent reports here now."""
    from cauce import compact, scribe

    source = scribe.take_relay(root, session_id, cwd)
    state = scribe.load(root, source) if source else None
    if source is None or state is None:
        return None
    if state.get("transcript"):
        state = scribe.update(root, source, state["transcript"]) or state
    moved = store.hand_over(source, session_id)
    folder = project.find(cwd)
    if orchestrator.is_it(folder, source):  # the role goes with the thread
        with contextlib.suppress(OSError):
            orchestrator.take(store, folder, session_id, cwd, via="relay", force=True)
    tasks = store.list_tasks(session_id=session_id, status=OPEN_FOR_A_RELAY, source=("cauce", "queue"),
                             limit=15)
    text = scribe.relay_text(state, tasks=tasks, changed=compact._changed(Path(cwd)),
                             commits=scribe.commits_since(state.get("cwd") or cwd, state.get("started")))
    if transcript:
        scribe.save(root, scribe.fresh(session_id, Path(transcript)) | {"relayed_from": source})
    if moved:
        text += (f"\n\n{moved} cauce task(s) that session sent (open, waiting on the person, or ended unheard) "
                 "report to this session now.")
    return text or None


def _orchestrates(store: Store, session_id: str, cwd: str) -> str | None:
    """At a session's start: whether it orchestrates this project (it is the one the
    project names, resumed; or it starts where that one ended), the work it takes
    from sessions gone quiet, and what the role asks of it."""
    folder = project.find(cwd)
    try:
        holds, moved = orchestrator.started(store, folder, session_id, cwd)
        if not holds and not orchestrator.is_it(folder, session_id):
            return None
        key = repo.key(cwd)
        moved += orchestrator.adopt(store, [key], session_id) if key else 0
    except OSError:
        return None
    text = ("cauce: this session orchestrates this project, and every stop is yours to carry. What only the "
            "person can grant (a refused command's rule, a cell's approval, a merge) you ask them for in this "
            "conversation and, on their yes, run its line; every other stop you decide: resume it, rewrite it, "
            "or dismiss it with why (`cauce dismiss N`). Nothing is left waiting on the person unasked.")
    if moved:
        text += f" {moved} task(s) sent by a session that ended or went quiet report here now."
    return text


def _enroll(cwd: str) -> None:
    """Using cauce in a project enrolls it: it gets its `.cauce/` folder."""
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(cwd)


def pre_tool_use(event: Mapping[str, Any], store: Store) -> dict | None:
    """A delegation to a subagent becomes a child of the turn that made it."""
    if event.get("tool_name") not in DELEGATION_TOOLS or not event.get("session_id"):
        return None
    parent = store.running_task(event["session_id"])
    tool_input = event.get("tool_input") or {}
    body = str(tool_input.get("prompt") or tool_input.get("description") or "delegated work")
    store.create_task(body, status="running", source="delegation", session_id=event["session_id"],
                      author="orchestrator", parent_id=parent["id"] if parent else None, cwd=event.get("cwd"),
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
    _revive(store, root, {**os.environ, **env})
    cwd = event.get("cwd")
    # The branches of this checkout whose work landed since: merged ones go.
    top = repo.toplevel(Path(cwd)) if cwd and Path(cwd).is_dir() else None
    if top is not None:
        _landed(store, top, root)
        flow.prune(store, [top])
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
    registry = _registry_notices(root)
    if registry:
        blocks.append(registry)
    if event.get("source") in ("resume", "compact"):
        # A turn or a subagent whose ending never arrived has nothing to resume: not unfinished work.
        unfinished = [t for t in store.list_tasks(session_id=session_id, status=("running", "interrupted"), limit=30)
                      if not (t.get("source") == "hook" and _ignored(str(t.get("body") or "")))
                      and (t["status"] == "running" or getattr(stops.of(store, t), "cause", None) != "turn")][:10]
        if unfinished:
            blocks.append("cauce: unfinished tasks in this session (context only, do not restart them unasked):\n"
                          + "\n".join(f"#{t['id']} [{t['status']}] {t['title']}" for t in unfinished))
        stopped = store.list_tasks(session_id=session_id, status=STOPPED_FOR_A_PERSON, limit=10,
                                   source=("cauce",))
        if stopped:
            blocks.append(_stopped_text(store, stopped))
    if event.get("source") == "compact":
        from cauce import compact

        kept = compact.recall(root, str(session_id))
        if kept:
            blocks.append(kept)
        _compaction_seen(store, root, str(session_id), "SessionStart")
    if event.get("source") in ("startup", "clear") and cwd and config.enabled("scribe", {**os.environ, **env}):
        relayed = _relay(store, root, str(session_id), str(cwd), event.get("transcript_path"))
        if relayed:
            blocks.append(relayed)
    if cwd:
        role = _orchestrates(store, str(session_id), str(cwd))
        if role:
            blocks.append(role)
    if event.get("source") == "startup":
        from cauce import scribe

        scribe.prune(root)
    # Only a project that uses cauce: indexing writes `.mcp-docs/` into the
    # checkout, and a session opened anywhere else is none of cauce's business.
    if cwd and repo.toplevel(Path(cwd)) is not None and project.find(cwd) is not None:
        blocks += _freshen(Path(cwd), root, env, adapters)
    if key:
        from cauce import cells

        dead = cells.recent_shared(store, repo=key, limit=5)
        if dead:
            blocks.append(_dead_end_text(dead, "cauce memory: recent fixes in this repository that did not work, "
                                               "met across areas."))
    if key:
        # What the store says is stuck now in this repository: a state cauce should never reach.
        from cauce import doctor

        broken = [f for f in doctor.check(store, repo=key, days=1) if f.severity == doctor.BROKEN]
        if broken:
            blocks.append("cauce doctor: work in this repository is stuck in a state cauce should never reach; "
                          "tell the person, and run each line only on their word:\n"
                          + "\n".join(f"- {f.what} → {f.fix}" for f in broken[:5]))
    if key and cwd and project.find(cwd) is not None and config.enabled("notes", {**os.environ, **env}):
        # After a compaction above all: what the project knows is read back from
        # its notes, not rebuilt from the code. The index, not the notes: a
        # session asks for what it needs with `cauce recall`.
        from cauce import notes

        notes.review(store, key, Path(cwd))
        index = notes.index_text(store, key)
        if index:
            blocks.append(index)
    return _context("SessionStart", "\n\n".join(blocks)) if blocks else None


def keep_digest(event: Mapping[str, Any], store: Store, root: Path) -> Path | None:
    """PreCompact: write the facts of the session (`cauce.compact`) that the
    SessionStart after the compaction puts back. Anywhere, notes setting or not:
    it is this session's own memory, not a project's."""
    session_id, transcript, cwd = event.get("session_id"), event.get("transcript_path"), event.get("cwd")
    if not (session_id and transcript):
        return None
    from cauce import compact

    tasks = store.list_tasks(session_id=str(session_id), limit=15)
    return compact.keep(root, str(session_id), Path(str(transcript)), Path(str(cwd)) if cwd else None, tasks)


def keep_notes(event: Mapping[str, Any], root: Path, env: Mapping[str, str], *,
               popen: Callable[..., Any] = subprocess.Popen) -> bool:
    """PreCompact and SessionEnd: start a detached `cauce extract-notes`, so what the
    conversation established is in the project's notes before it is summarized
    away or closed. A model call takes longer than a hook may; one at a time per
    session. Only in an enrolled project, with the `notes` setting on."""
    session_id, transcript, cwd = event.get("session_id"), event.get("transcript_path"), event.get("cwd")
    if not (session_id and transcript and cwd) or not config.enabled("notes", env):
        return False
    if project.find(cwd) is None or not Path(str(transcript)).is_file():
        return False
    from cauce import dispatch

    if dispatch.held(root, f"notes:{session_id}"):
        return False
    try:
        (root / "work").mkdir(parents=True, exist_ok=True)
        with open(root / "work" / "notes.log", "a") as out:
            popen([sys.executable, "-m", "cauce", "extract-notes", str(session_id), str(transcript), str(cwd)],
                  cwd=str(cwd), env=dispatch.child_env(env), stdin=subprocess.DEVNULL, stdout=out,
                  stderr=subprocess.STDOUT, start_new_session=True)
    except OSError:
        return False
    return True


#: Endings of cauce runs that wait on a person to clear something.
STOPPED_FOR_A_PERSON = ("blocked", "needs_approval")


def _endings(store: Store, session_id: str, *, via: str, mark: bool = True) -> str | None:
    """Work this session sent that ended since it last heard, each with what to do
    next, marked as reported: a queued task finishes while nobody waits on it,
    and the session that sent it would otherwise never learn it should act.
    `mark=False` reads them without taking them: a display, not a delivery."""
    return endings(store, session_id, claim=via if mark else None)[0]


def endings(store: Store, session_id: str, *, claim: str | None = None) -> tuple[str | None, list[int]]:
    """The notice of what ended since the session last heard, and the tasks it
    covers. With `claim`, they are marked delivered by that route as they are
    read, atomically; without, only read."""
    ended = store.claim_unreported(session_id, via=claim) if claim else store.unreported(session_id)
    if not ended:
        return None, []
    lines = [ENDINGS_HEADER + " Act on each now: tell the person what happened. Every stop is yours to carry: "
             "one only the person can grant (a refused command's rule, a cell's approval) you ask them for here, "
             "and on their yes you run its line; any other you decide as its line says: resume it, rewrite it, "
             "or dismiss it with why (`cauce dismiss N`). Nothing waits on the person unasked (never redo a "
             "worker's task by hand)."]
    for t in ended:
        data = t["finished_data"]
        changed = data.get("changed") or []
        total = data.get("changed_n") or len(changed)
        stop = stops.view(store, t) if t["status"] != "done" else None
        decided = bool(stop) and stop["cause"] == "decision"
        # Work done that waits on one decision is no failure, and its header never reads as one.
        lines.append(f"#{t['id']} [{'work done, one decision open' if decided else t['status']}] {t['title']}")
        # A security problem the worker named goes first, on a line of its own, secrets masked.
        lines += [f"  ⚠ security: {alert}" for alert in data.get("alerts") or ()]
        # Said when it started, to a caller that may be gone by now: a queued step's above all.
        lines += [f"  it ran with a warning: {warning}" for warning in _warned(store, t["id"])]
        lines += _went_past(store, t)
        lines += _behind(store, t)
        lines += _found_before(store, t)
        if t["status"] == "done":
            branch = data.get("branch")
            commit = f" (commit {str(data['commit'])[:12]})" if data.get("commit") else ""
            # In the checkout only when the run had no worktree; an older ending did not say.
            where = (f"; its work is on branch {branch}{commit}, merged only on the person's word"
                     if branch else "; its changes are in the checkout (the run had no worktree)"
                     if changed and not data.get("isolated") else "; no branch kept its changes"
                     if changed else "; it changed nothing")
            lines.append(f"  passed at {data.get('final_cell') or t['final_cell']}{where}")
            if t.get("result"):
                lines.append(f"  the worker says: {str(t['result'])[:800]}")
            if changed:
                lines.append("  changed: " + ", ".join(changed[:8])
                             + (f" and {total - 8} more" if total > 8 else ""))
            lines += verdict_lines(data.get("verdict"))
            continue
        if decided:
            branch = data.get("branch")
            commit = f" (commit {str(data['commit'])[:12]})" if data.get("commit") else ""
            checked = {"cauce": "its check passed, run by cauce",
                       "worker": "its own check passed, not re-run by cauce"}.get(str(stop.get("checked") or ""),
                                                                                 "no check cauce ran passed")
            lines.append(f"  the work is done and kept on branch {branch}{commit}, merged only on the person's word "
                         f"({checked})" if branch else f"  the work is done and in the checkout ({checked})")
            said = stop.get("account") or stop["reason"]
            lines.append(f"  one point needs a decision; the worker says: {said}"[:900])
        elif stop:
            lines.append(f"  stopped by {stop['who']}: {stop['reason'][:300]}")
            if stop.get("account"):
                lines.append(f"  the worker found: {stop['account'][:800]}")
        if changed:
            shown = ", ".join(changed[:5]) + (f" and {total - 5} more" if total > 5 else "")
            lines.append(f"  changed so far: {shown}")
        if stop:
            whose = "ask the person for it here" if stops.waits_on(store, t) == "person" else "yours to decide"
            lines.append(f"  next ({whose}): {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
        waiting = store.lane_paused_on(t.get("repo"), t["id"])
        if waiting:
            lines.append(f"  its lane is paused on it: {waiting} queued task(s) wait. Dismissing or resuming it "
                         "opens the lane; when the queued work does not depend on it, tell the person and "
                         f"run `cauce lanes --unpause {t.get('repo')}` to let them start now")
    return "\n".join(lines), [t["id"] for t in ended]


def verdict_lines(verdict: Mapping | None) -> list[str]:
    """The verdict a passing ending carries (`orchestrate.verdict_card`), for the session that
    sent the work: what it was held to and how that was checked, so it reports the result
    instead of re-reading the code or running the tests again. A verdict cauce did not keep
    (an older ending) says nothing."""
    if not isinstance(verdict, Mapping):
        return []
    lines = []
    check = verdict.get("check")
    lines.append(f"  check: `{check['command']}` passed, run by cauce in its worktree" if isinstance(check, Mapping)
                 else "  check: none was given, so nothing ran it: say so to the person")
    for c in verdict.get("criteria") or ():
        mark = "met" if c.get("met") else "NOT met"
        lines.append(f"  {c.get('id')} {mark}: {c.get('evidence') or 'no evidence given'}")
    ind = verdict.get("independent") or {}
    if ind.get("ran"):
        lines.append("  independent check: " + ("passed" if ind.get("passed") else
                                                 f"{ind.get('blocking')} blocker(s) left, recorded"))
    elif ind.get("why"):
        lines.append(f"  independent check: not run ({ind['why']})")
    unverified = not isinstance(check, Mapping) or any(not c.get("met") for c in verdict.get("criteria") or ())
    lines.append("  this verdict is cauce's own record: do not re-read the code or re-run the check to confirm it"
                 + ("; open the diff only where it says something was not checked or not met" if unverified
                    else "; open the diff only if the person asks"))
    return lines


#: The most problems that were already there an ending lists.
FOUND_BEFORE_SHOWN = 3


def _found_before(store: Store, ended: Mapping) -> list[str]:
    """Problems the work found that were already there, shown with proof: never this change's
    to fix, never a blocker, and so never fixed unless somebody starts it. The ending names
    them and the bugfix that would take one on, after this work, for the orchestrator to
    decide; nothing is started."""
    tasks = store.run_tasks(int(ended["workflow_run"])) if ended.get("workflow_run") else [ended]
    found: dict[str, Mapping] = {}
    for task in tasks:
        final = store.final_event(task["id"]) if task.get("status") == "done" else None
        for f in ((final or {}).get("data") or {}).get("findings") or ():
            if isinstance(f, Mapping) and f.get("cause") == "pre-existing" and f.get("evidence_class") == \
                    "deterministic":
                found.setdefault(findings_mod.key(f), f)
    if not found:
        return []
    after_ref = f"run:{ended['workflow_run']}" if ended.get("workflow_run") else f"task:{ended['id']}"
    shown = list(found.values())[:FOUND_BEFORE_SHOWN]
    lines = [f"  it found {len(found)} problem(s) that were already there, not this change's (each shown with "
             "its proof); a bugfix for one is yours to decide, nothing was started:"]
    for f in shown:
        # A finding may quote what it found: nothing that looks like a secret is repeated.
        what = _SECRET.sub("[secret]", " ".join(str(f.get("what") or "").split()))[:200]
        where = str(f.get("where") or "")
        lines.append(f"    {where}: {what} — `cauce workflow run bugfix {shlex.quote(f'{what} ({where})')} "
                     f"--after {after_ref}`")
    if len(found) > len(shown):
        lines.append(f"    and {len(found) - len(shown)} more: `cauce show {ended['id']}`")
    return lines


def _behind(store: Store, ended: Mapping) -> list[str]:
    """How far each step of the ended task's workflow run is behind the branch the run
    started from, as last measured (its start, or the run's end)."""
    if not ended.get("workflow_run"):
        return []
    from cauce import isolate

    lines = []
    for step in store.run_tasks(int(ended["workflow_run"])):
        kept = store.last_event(step["id"], "behind_base")
        said = isolate.behind_text(kept["data"]) if kept else None
        if said:
            lines.append(f"  step {step['workflow_step']} (#{step['id']}): {said}")
    return lines


def _warned(store: Store, task_id: int) -> list[str]:
    """What a task's plan warned of when its run started, clipped."""
    planned = (store.last_event(task_id, "planned") or {}).get("data") or {}
    return [str(warning)[:300] for warning in planned.get("warnings") or ()]


def _went_past(store: Store, ended: Mapping) -> list[str]:
    """The warnings of the steps of a workflow run the engine went past since the
    session last heard of that run: their endings are never handed over, so the
    run's next one says them."""
    if not ended.get("workflow_run"):
        return []
    heard, past = 0, []
    for step in store.run_tasks(int(ended["workflow_run"])):
        finished = store.last_event(step["id"], "finished")
        if step["id"] == ended["id"] or not finished or finished["id"] > ended["finished_id"]:
            continue
        via = {e["data"].get("via") for e in store.events_of(step["id"], "reported")}
        if via - {"workflow"}:
            heard = max(heard, finished["id"])
        elif via:
            past.append((finished["id"], step))
    return [f"  step {step['workflow_step']} (#{step['id']}) ran with a warning: {warning}"
            for at, step in past if at > heard for warning in _warned(store, step["id"])]


def _stopped_text(store: Store, tasks: list[dict]) -> str:
    """The session's runs that stopped, with why and what continues each: the session
    carries every one, asking the person only for what only they can grant."""
    lines = ["cauce: tasks of this session that stopped; each is yours to carry (decide it, or ask the person "
             "here for what only they can grant):"]
    for t in tasks:
        stop = stops.view(store, t)
        lines.append(f"#{t['id']} [{t['status']}] {t['title']}")
        if stop:
            whose = "ask the person for it here" if stops.waits_on(store, t) == "person" else "yours to decide"
            lines.append(f"  stopped by {stop['who']}: {stop['reason'][:300]}")
            lines.append(f"  next ({whose}): {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
    return "\n".join(lines)


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
        adapters = default_adapters(env)
    notes = []
    for adapter in adapters:
        if not getattr(adapter, "indexes", True):
            continue
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


#: Hooks that need the environment and the cauce home, not only the store.
WITH_ENV = frozenset({"SessionStart", "PreCompact", "PostCompact", "SessionEnd"})


def handle(
    event_name: str, event: Mapping[str, Any], store: Store, root: Path, env: Mapping[str, str] | None = None
) -> dict | None:
    if event_name == "SessionStart":
        return session_start(event, store, root, env)
    if event_name == "PreCompact":
        keep_digest(event, store, root)
    if event_name in ("PreCompact", "PostCompact") and event.get("session_id"):
        # Whatever asked for it: the mod reads these to learn whether a `/compact`
        # it asked for ran, and every compaction starts the cooldown.
        _compaction_seen(store, root, str(event["session_id"]), event_name, trigger=event.get("trigger"),
                         instructions=event.get("custom_instructions"))
    if event_name == "SessionEnd" and event.get("cwd"):
        with contextlib.suppress(OSError):
            orchestrator.ended(project.find(str(event["cwd"])), event.get("session_id"), str(event.get("reason") or ""))
    if event_name in ("PreCompact", "SessionEnd"):
        keep_notes(event, root, dict(os.environ if env is None else env))
        return None
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
        note_error(root, event_name)
    return 0


def note_error(root: Path, event_name: str) -> None:
    """Append the exception being handled to the error log the next SessionStart reports."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (root / ERROR_LOG).open("a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp} {event_name}\n{traceback.format_exc()}\n")
    except Exception:  # noqa: S110 - losing a note is acceptable, raising from here is not
        pass


#: The registry notices a SessionStart last said, so the same ones are said once.
REGISTRY_SAID = "capabilities.said"


def _registry_notices(root: Path) -> str | None:
    """What the person's capability registry does that they may not mean
    (`capabilities.notices`), said at the first SessionStart that sees it, and again
    only when it changes."""
    from cauce import capabilities

    lines = capabilities.notices(root / "capabilities.json")
    said = root / REGISTRY_SAID
    text = "\n".join(lines)
    try:
        if text == (said.read_text(encoding="utf-8") if said.is_file() else ""):
            return None
        said.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return text or None


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
