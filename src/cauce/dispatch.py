"""Whether a queued task can start beside the work already going in its repository.

Each writing task gets its own git worktree, so two tasks never write into the
same checkout. What parallel work can still break is the *result*: a task that
needs another's change, or two that edit the same behaviour and leave branches
that do not merge. That is a question about meaning, not about files listed in
advance, so Haiku answers it — with a JSON schema and no tools, like the
classifier.

The decision is made once, at dispatch time, against what is running and what
was queued ahead of the task, and it is kept on the task with its reason. It
fails closed: a model that cannot be run, or does not answer, means the task
waits its turn. Waiting costs time; a wrong "parallel" costs a broken merge.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cauce import after, config
from cauce.classify import ask_haiku
from cauce.isolate import PATHLIKE
from cauce.store import Store, home
from cauce.text import fold

#: How much of each task's text the model reads: all of it when it fits, else its
#: opening and then the lines that carry a coordination fact, which a long text
#: (a workflow step's, a feature's spec) puts anywhere, often last.
BODY_CHARS = 1200
OPENING_CHARS = 400
#: The most one such line takes, cut around what made it one.
FACT_CHARS = 200
#: The most files a running task changed that the question names.
CHANGED_FILES = 40

#: A coordination fact, read on the folded text (`text.fold`: `migración` is
#: `migracion`), in two ranks: a migration or work beside it (`parallel`,
#: `paralelo`) says how the task takes its place among the others; a version or
#: another task by its number only may, since a spec mentions versions of
#: everything. A line naming a file (`isolate.PATHLIKE`) is one too, ranked last:
#: a long text names many files.
_DECLARES = re.compile(r"\bmigra|\bparal+el")
_MENTIONS = re.compile(r"\bversion|#\d+")
#: Where a task's text is cut into lines: its line breaks, and its sentences (a
#: clause after `;` stays with the sentence it belongs to).
_SEGMENT = re.compile(r"\n+|(?<=[.!?])\s+")

SYSTEM_PROMPT = (
    "You decide whether a new software task can start now, at the same time as other tasks in "
    "the same repository, or must wait until they finish. Each task runs in its own copy of the "
    "repository and its changes are merged afterwards. Answer parallel=true only when the new "
    "task needs nothing another task will produce, and is unlikely to change the same files, the "
    "same functions or the same behaviour as any of them. Reading-only tasks (questions, reviews, "
    "searches) can always run in parallel. Each task is shown by the opening of its text and the "
    "lines further on (marked …) that say how it coordinates with other work. What a task's text "
    "declares is a fact: a migration or version number it gives itself, while the other tasks take "
    "the other numbers, is no dependency on the tasks numbered before it; only what its text says "
    "it needs from another task is. The surfaces a task declared are the only files it may change, "
    "and the files a running task changed so far are read from git: judge overlap on those, not on "
    "guesses. When unsure, answer false. The reason is one short sentence naming the task it "
    "conflicts with, if any."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "parallel": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["parallel", "reason"],
    "additionalProperties": False,
}


#: Tags the model leaked past its answer ("…as #55.</reason> </invoke>"), and a closing
#: tag cut off at the end. A bare `<` in prose ("a < b") is no tag and stays.
_MARKUP = re.compile(r"</?[A-Za-z][^<>]*>|</[^<>]*$")
REASON_CHARS = 200


@dataclass(frozen=True)
class Decision:
    parallel: bool
    reason: str
    cost_usd: float = 0.0


def declared_surfaces(task: Mapping[str, Any]) -> tuple[str, ...]:
    """The surfaces a task was queued with (`--surface`, a workflow step's)."""
    try:
        return tuple(json.loads(task.get("options") or "{}").get("surfaces") or ())
    except ValueError:
        return ()


def _clip(line: str, at: int) -> str:
    """`line` in at most `FACT_CHARS`, keeping what sits at `at` and a little before it."""
    if len(line) <= FACT_CHARS:
        return line
    start = max(0, min(at - FACT_CHARS // 4, len(line) - FACT_CHARS + 2))
    end = start + FACT_CHARS - 2
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def _excerpt(task: Mapping[str, Any]) -> tuple[str, list[str]]:
    """What the model reads of a task's text, whitespace collapsed: all of it when it
    fits in `BODY_CHARS`; else its opening, and the lines past it that carry a
    coordination fact (those declaring a migration or parallel work first, then those mentioning a
    version or another task, then those naming a file), in
    the text's order, as many as fit in the same bound."""
    raw = str(task.get("body") or task.get("title") or "")
    whole = " ".join(raw.split())
    if len(whole) <= BODY_CHARS:
        return whole, []
    opening = whole[:OPENING_CHARS - 1] + "…"
    found, offset = [], 0
    for n, line in enumerate(" ".join(part.split()) for part in _SEGMENT.split(raw) if part.strip()):
        offset += len(line) + 1
        if offset - 1 <= OPENING_CHARS - 1:
            continue  # read whole in the opening
        folded = fold(line)
        if hit := _DECLARES.search(folded):
            rank = 0
        elif hit := _MENTIONS.search(folded):
            rank = 1
        elif hit := PATHLIKE.search(line):
            rank = 2
        else:
            continue
        found.append((rank, n, _clip(line, hit.start())))
    room, kept = BODY_CHARS - len(opening), []
    for _, n, line in sorted(found):
        if len(line) + 3 <= room:
            room -= len(line) + 3
            kept.append((n, line))
    return opening, [line for _, line in sorted(kept)]


def _files(paths: Sequence[str]) -> str:
    if not paths:
        return "none yet"
    more = len(paths) - CHANGED_FILES
    return ", ".join(paths[:CHANGED_FILES]) + (f" and {more} more" if more > 0 else "")


def _described(task: Mapping[str, Any]) -> list[str]:
    opening, facts = _excerpt(task)
    lines = [opening, *(f"… {fact}" for fact in facts)]
    if declared := declared_surfaces(task):
        lines.append("Surfaces it declared: " + ", ".join(declared))
    if task.get("changed") is not None:
        lines.append("Files it changed so far (git): " + _files(task["changed"]))
    return lines


def question(task: Mapping[str, Any], ahead: Sequence[Mapping[str, Any]]) -> str:
    """The new task and the work ahead of it, each as `_excerpt` reads it, with the
    surfaces it declared and, for a running task whose worktree git could read
    (`changed`, which the dispatcher sets), the files it changed so far."""
    lines = [f"New task #{task['id']}:", *_described(task), "", "Already running or queued ahead of it:"]
    for t in ahead:
        opening, *rest = _described(t)
        lines += [f"- #{t['id']} ({t['status']}): {opening}", *(f"  {line}" for line in rest)]
    return "\n".join(lines)


def _plain(reason: object) -> str:
    """A reason as the board shows it: text on one line, with no markup the model leaked."""
    return " ".join(_MARKUP.sub(" ", str(reason or "")).split())[:REASON_CHARS].rstrip()


def decide(task: dict, ahead: Sequence[dict], **haiku_kwargs) -> Decision:
    """Never raises. Nothing ahead means nothing to conflict with, and no call."""
    if not ahead:
        return Decision(True, "nothing runs or waits ahead of it")
    out, cost, failure = ask_haiku(SYSTEM_PROMPT, SCHEMA, question(task, ahead), **haiku_kwargs)
    if out is None or not isinstance(out.get("parallel"), bool):
        return Decision(False, f"waits its turn: {failure or 'the model answered outside the schema'}", cost)
    reason = _plain(out.get("reason"))
    return Decision(out["parallel"], reason or ("independent" if out["parallel"] else "may conflict"), cost)


def waiting(store: Store, task: Mapping) -> str | None:
    """What a queued task the dispatcher held back waits for now, in one line
    ("waits for #46: <reason>"); None for one it has not held back, or with
    nothing left ahead of it. The decision is made once, but the work it waits
    for moves on: it is read from the lane as `flow.runnable` holds the task,
    until nothing the dispatcher started runs there and nothing queued is ahead.
    Work queued after other work says that first ("waits for #17 to pass", `after`)."""
    if (held := after.waiting(store, task)) is not None:
        return held
    if task.get("status") != "queued" or task.get("parallel") != 0:
        return None
    ahead = [t["id"] for t in store.dispatched_running(task.get("repo"))]
    ahead += [t["id"] for t in store.queued_in(task.get("repo")) if t["id"] < task["id"] and not after.held(t)]
    if not ahead:
        return None
    decided = (store.last_event(task["id"], "dispatch_decided") or {}).get("data") or {}
    reason = task.get("parallel_reason") or decided.get("reason") or "it may conflict with the work ahead"
    reason = reason.removeprefix("waits its turn: ")  # a model that did not answer: the line says it waits
    # The task the decision named, else one it was judged against, else the first now
    # ahead; whenever that is not the one the decision named, the reason says whom it
    # was about. A decision kept before the event existed names none.
    judged = decided.get("behind")
    behind = next((i for i in [judged, *(decided.get("ahead") or ())] if i in ahead), ahead[0])
    if judged and behind != judged:
        reason += f" (judged against #{judged})"
    return f"waits for #{behind}: {reason}"


# --- the dispatcher process -------------------------------------------------
#
# One `cauce work` per repository at a time, held by a lock file. A task queued
# with `++` starts one if none holds the lock; a second one started in the same
# instant finds the lock taken and exits. This module stays light: the prompt
# hook imports it.

def lock_path(root: Path, scope: str) -> Path:
    digest = hashlib.sha256(scope.encode()).hexdigest()[:16]
    return root / "work" / f"{digest}.lock"


@contextlib.contextmanager
def hold(root: Path, scope: str) -> Iterator[bool]:
    """Yields True while this process is the dispatcher for `scope`, False if another one is."""
    path = lock_path(root, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def held(root: Path, scope: str) -> bool:
    """Whether a dispatcher runs for `scope` now."""
    with hold(root, scope) as mine:
        return not mine


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a process cauce starts on its own: it can import cauce,
    and it carries no session's identity — the work is not that session's turn."""
    out = dict(os.environ if env is None else env)
    src = str(Path(__file__).resolve().parents[1])
    out["PYTHONPATH"] = src + (os.pathsep + out["PYTHONPATH"] if out.get("PYTHONPATH") else "")
    out.pop("CAUCE_SESSION_ID", None)
    return out


def kick(cwd: str | Path | None, key: str | None, env: Mapping[str, str]) -> str:
    """Start the repository's dispatcher for work just queued (the `autowork`
    setting), and say what happens next. A `++` and a `cauce queue add` both
    come here: a task a session queued by hand otherwise waits for a `cauce
    work` nobody runs, or one run in the session's background, which dies
    with the session."""
    if not cwd or not config.enabled("autowork", env):
        return "`cauce work` runs the queue."
    root = home(dict(env))
    if held(root, key or "*") or start(Path(cwd), root, key or "*"):
        if config.enabled("parallel", env):
            return "A worker takes it now, beside the running ones if Haiku finds it independent, or after them."
        return "A worker takes it when the tasks ahead of it are done."
    return "Starting a worker failed; `cauce work` runs the queue."


def reopened(task: Mapping, env: Mapping[str, str]) -> None:
    """After a person took a lane's stopping task off the board, start the queue
    behind it again: its dispatcher stopped when the lane paused. Never raises."""
    with contextlib.suppress(Exception):
        if task.get("cwd") and Path(str(task["cwd"])).is_dir():
            kick(str(task["cwd"]), task.get("repo"), env)


def start(repo_dir: Path, root: Path, scope: str, *, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Start a detached `cauce work` for a repository, unless one already runs. Never raises."""
    try:
        if held(root, scope):
            return False
        log = root / "work" / "dispatcher.log"
        with open(log, "a") as out:
            popen([sys.executable, "-m", "cauce", "work", "--repo", str(repo_dir), "--detached"], cwd=str(repo_dir),
                  env=child_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                  start_new_session=True)
        return True
    except OSError:
        return False


def start_lane(store: Store, root: Path, key: str | None, *,
               popen: Callable[..., object] | None = None) -> bool:
    """Start the dispatcher of a lane that has queued work, unless one holds it: the
    first queued task that may run (none held behind other work) says where, by its
    directory. False when nothing is queued with a directory to start in, or a
    dispatcher already runs. A restart leaves queued work with no `cauce work` to run
    it; this is how a person's unpause or a resume starts one."""
    queued = [t for t in store.queued_in(key or None) if not after.held(t)]
    where = Path(queued[0]["cwd"]) if queued and queued[0]["cwd"] else None
    if where is None or not where.is_dir():
        return False
    return start(where, root, key or "*", **({"popen": popen} if popen is not None else {}))


#: A lane is revived at most this often: a hook runs at every turn's end.
REVIVE_EVERY_S = 60


def revive(store: Store, root: Path, env: Mapping[str, str], *,
           popen: Callable[..., object] | None = None, now: float | None = None) -> list[str | None]:
    """Start a dispatcher for every lane with queued work that may run and none to run it
    (the `autowork` setting): one that died (a full disk, a killed process) leaves its
    queue still until somebody types `cauce work`. The hooks call it; never a GET. A lane
    paused on a person, or whose work is all held behind other work, is left alone, and
    each lane is tried at most every `REVIVE_EVERY_S`. The lanes it started a dispatcher for."""
    if not config.enabled("autowork", env) or held(root, "*"):
        return []
    now = time.time() if now is None else now
    revived: list[str | None] = []
    for lane in store.lanes():
        key = lane["repo"] or None
        if not lane["queued"] or lane["paused"] or held(root, key or "*"):
            continue
        stamp = lock_path(root, key or "*").with_suffix(".revived")
        with contextlib.suppress(OSError):
            if now - stamp.stat().st_mtime < REVIVE_EVERY_S:
                continue
        if start_lane(store, root, key, popen=popen):
            with contextlib.suppress(OSError):
                stamp.touch()
                os.utime(stamp, (now, now))
            revived.append(key)
    return revived
