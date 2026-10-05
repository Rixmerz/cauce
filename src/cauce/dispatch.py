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
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cauce.classify import ask_haiku

#: How much of each task the model reads. Enough to see what it touches.
BODY_CHARS = 600

SYSTEM_PROMPT = (
    "You decide whether a new software task can start now, at the same time as other tasks in "
    "the same repository, or must wait until they finish. Each task runs in its own copy of the "
    "repository and its changes are merged afterwards. Answer parallel=true only when the new "
    "task needs nothing another task will produce, and is unlikely to change the same files, the "
    "same functions or the same behaviour as any of them. Reading-only tasks (questions, reviews, "
    "searches) can always run in parallel. When unsure, answer false. The reason is one short "
    "sentence naming the task it conflicts with, if any."
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


@dataclass(frozen=True)
class Decision:
    parallel: bool
    reason: str
    cost_usd: float = 0.0


def _excerpt(task: dict) -> str:
    body = " ".join(str(task.get("body") or task.get("title") or "").split())
    return body if len(body) <= BODY_CHARS else body[:BODY_CHARS - 1] + "…"


def question(task: dict, ahead: Sequence[dict]) -> str:
    lines = [f"New task #{task['id']}:", _excerpt(task), "", "Already running or queued ahead of it:"]
    lines += [f"- #{t['id']} ({t['status']}): {_excerpt(t)}" for t in ahead]
    return "\n".join(lines)


def decide(task: dict, ahead: Sequence[dict], **haiku_kwargs) -> Decision:
    """Never raises. Nothing ahead means nothing to conflict with, and no call."""
    if not ahead:
        return Decision(True, "nothing runs or waits ahead of it")
    out, cost, failure = ask_haiku(SYSTEM_PROMPT, SCHEMA, question(task, ahead), **haiku_kwargs)
    if out is None or not isinstance(out.get("parallel"), bool):
        return Decision(False, f"waits its turn: {failure or 'the model answered outside the schema'}", cost)
    reason = " ".join(str(out.get("reason") or "").split())[:200]
    return Decision(out["parallel"], reason or ("independent" if out["parallel"] else "may conflict"), cost)


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


def start(repo_dir: Path, root: Path, scope: str, *, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Start a detached `cauce work` for a repository, unless one already runs. Never raises."""
    try:
        if held(root, scope):
            return False
        log = root / "work" / "dispatcher.log"
        with open(log, "a") as out:
            popen([sys.executable, "-m", "cauce", "work", "--repo", str(repo_dir)], cwd=str(repo_dir),
                  env=child_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                  start_new_session=True)
        return True
    except OSError:
        return False
