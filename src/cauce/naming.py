"""Haiku names a session after what it has been asked, in `.cauce/sessions.json`.

The Stop hook decides, from the file and a count, whether a session wants a
name (`project.wants_name`) and starts `cauce name-session` detached: a model
call takes longer than a hook may. One naming per session at a time, under a
lock. A session a person renamed is never renamed again.
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from cauce import config, dispatch, project
from cauce.store import Store, now

SYSTEM_PROMPT = (
    "You name a Claude Code session after the work it is about, so a person can find it in a list. "
    "You get its first prompts and its latest ones. Answer with a name of two to six words, in the "
    "language the prompts are written in, naming the work (not the person, not the tool): for "
    "example 'Parallel dispatcher for the queue' or 'Arreglo del login con Google'. No quotes, no "
    "trailing period. Prompts may be cut short or say little: name what they show, even if it is "
    "only 'Checking queued cauce tasks'. Always answer with a name, never with why you cannot."
)

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}

#: How many prompts from each end of a session the model reads.
EDGE = 6
MAX_NAME = 80
#: How much of each prompt the model reads.
PROMPT_CHARS = 300


def _prompts(store: Store, session_id: str) -> list[str]:
    tasks = store.list_tasks(session_id=session_id, source=("hook",), limit=100000)
    # oldest first; the text itself, not the title, which cuts it at a few words
    return [" ".join(str(t["body"] or t["title"]).split())[:PROMPT_CHARS] for t in reversed(tasks)]


def question(prompts: list[str]) -> str:
    first, last = prompts[:EDGE], prompts[EDGE:][-EDGE:]
    lines = ["Name the session these prompts come from.", "", "First prompts:", *[f"- {p}" for p in first]]
    if last:
        lines += ["", "Latest prompts:", *[f"- {p}" for p in last]]
    return "\n".join(lines)


def name_session(store: Store, session_id: str, **haiku_kwargs) -> str | None:
    """Ask Haiku for a name and write it. Returns it, or None when nothing was written."""
    from cauce.classify import ask_haiku

    session = store.get_session(session_id)
    folder = project.find(session["cwd"]) if session else None
    if folder is None:
        return None
    prompts = _prompts(store, session_id)
    if not project.wants_name(project.names(folder).get(session_id), len(prompts)):
        return None
    out, _cost, _failure = ask_haiku(SYSTEM_PROMPT, SCHEMA, question(prompts), **haiku_kwargs)
    name = " ".join(str((out or {}).get("name") or "").split()).strip("\"'. ")[:MAX_NAME]
    if not name:
        return None
    return name if project.set_haiku_name(folder, session_id, name, len(prompts), now()) else None


def maybe_start(
    store: Store,
    session_id: str,
    root: Path,
    env: Mapping[str, str],
    *,
    popen: Callable[..., object] = subprocess.Popen,
) -> bool:
    """From the Stop hook: start a detached naming when the session wants one. Never raises."""
    try:
        if not config.enabled("names", env):
            return False
        session = store.get_session(session_id)
        folder = project.find(session["cwd"]) if session else None
        if folder is None:
            return False
        count = len(store.list_tasks(session_id=session_id, source=("hook",), limit=100000))
        if not project.wants_name(project.names(folder).get(session_id), count):
            return False
        if dispatch.held(root, f"name:{session_id}"):
            return False
        with open(root / "work" / "naming.log", "a") as out:
            popen([sys.executable, "-m", "cauce", "name-session", session_id], cwd=str(folder.parent),
                  env=dispatch.child_env(env), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                  start_new_session=True)
        return True
    except OSError:
        return False
