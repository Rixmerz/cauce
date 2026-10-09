"""Habits: repeated tool sequences, mined, and turned into automation only with a person's say.

The idea is muscle-memory's; the code is cauce's. Every tool call is recorded
as a **signature** — the verb and the kind of thing it acted on, `bash:pytest`,
`edit:.py` — plus a hash of its arguments. Never the arguments, the file
contents, the output or the prompt: the miner needs to know *that* the
formatter ran after a Python edit, not what was in either. A last hard filter
keeps the signature to `[a-z0-9:._-]`, so a case the scrubbing missed still
cannot carry a path or a secret.

From those records:

- **candidates** — sequences that repeat *and could run on their own*: they
  start with an edit or a write and end in a command that does something (a
  formatter, a linter, tests, a build). The model looking around — `find → ls`,
  `cat → cd` — repeats in every session and is no habit: a hook could not take
  it over or save a turn. Gates are boolean (automatable, enough occurrences,
  in enough sessions, nearly always succeeding, nothing destructive); the score
  only ranks what passed them. A large count must never buy its way past a gate.
- **recipes** — per kind of task, the sequences that came before a worker's
  pass. They go into the next worker's brief: the steps that worked here last
  time, so it does not spend turns rediscovering them.
- **automation** — a candidate becomes a `PostToolUse` hook only when a person
  runs `cauce habits install` with the command to run. Nothing in cauce
  installs one by itself. An installed habit that fails three times in a row
  turns itself off, and `cauce habits uninstall` removes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cauce import signature as sig
from cauce.signature import arg_hash, signature  # noqa: F401 - re-exported for callers and tests
from cauce.store import Store

#: The steps a hook can be triggered by: an edit or a write of a file type.
_EDIT_TRIGGER = re.compile(r"^(edit|write):(.+)$")
#: Signatures a candidate may never contain: automation must not destroy.
_DESTRUCTIVE = re.compile(r"bash:(rm|git-push|git-reset|git-clean|git-checkout|docker-rm|kubectl-delete|"
                          r"terraform-apply|terraform-destroy|dd|mkfs|shutdown|reboot)")

#: Bash programs that look, move around or print: a sequence of these is the
#: model finding its way, never a step worth automating. `find → ls` repeating
#: in every session is how exploring looks, not a habit a hook could take over.
_LOOKING = frozenset({
    "ls", "find", "fd", "cat", "bat", "head", "tail", "less", "more", "cd", "pushd", "popd", "pwd", "echo",
    "printf", "grep", "rg", "ag", "egrep", "wc", "tree", "which", "type", "file", "stat", "du", "df", "sort",
    "uniq", "cut", "tr", "diff", "sed", "awk", "jq", "xargs", "sleep", "date", "env", "export", "true", "test",
    "mkdir", "touch", "cp", "mv", "ln", "chmod", "realpath", "dirname", "basename", "readlink", "unknown",
    "git", "git-status", "git-log", "git-diff", "git-show", "git-branch", "git-remote", "git-rev-parse",
    "git-ls-files", "git-blame", "git-config", "git-stash", "git-fetch", "git-add",
})

MIN_OCCURRENCES = 3
MIN_SESSIONS = 2
MIN_SUCCESS = 0.9
MAX_LENGTH = 4
MAX_FAILURES = 3

def load_events(store: Store, env: Mapping[str, str] | None = None) -> int:
    """Move new lines from the hooks' append-only logs into the database."""
    directory = sig.log_dir(dict(os.environ) if env is None else env)
    if not directory.is_dir():
        return 0
    count = 0
    for path in sorted(directory.glob("*.ndjson")):
        offset = store.transcript_offset(str(path))
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    record = json.loads(raw)
                    store.add_tool_event(**{k: record.get(k) for k in (
                        "ts", "session_id", "task_id", "attempt", "tool", "sig", "arg_hash", "ok")})
                    count += 1
                except (ValueError, TypeError):
                    continue
        store.set_transcript_offset(str(path), offset)
    return count


# --- mining -----------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    steps: tuple[str, ...]
    occurrences: int
    sessions: int
    success: float
    distinct_args: int
    score: float

    @property
    def id(self) -> str:
        return hashlib.sha256(" > ".join(self.steps).encode()).hexdigest()[:8]

    def line(self) -> str:
        return (f"{self.id}  {' → '.join(self.steps)}  ×{self.occurrences} in {self.sessions} sessions, "
                f"{self.success:.0%} ok")


def _runs(events: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for e in events:
        by_run[f"{e['session_id']}:{e['task_id']}:{e['attempt']}"].append(e)
    return by_run


def candidates(events: Sequence[Mapping[str, Any]]) -> list[Candidate]:
    """Sequences of 2–4 consecutive signatures that pass every gate, best first."""
    occurrences: dict[tuple[str, ...], list[tuple[str, bool, str]]] = defaultdict(list)
    for run_key, run in _runs(events).items():
        session = run_key.split(":", 1)[0]
        sigs = [e["sig"] for e in run]
        for length in range(2, MAX_LENGTH + 1):
            for i in range(len(sigs) - length + 1):
                steps = tuple(sigs[i:i + length])
                if len(set(steps)) == 1:
                    continue  # the same call repeated is a loop, not a habit
                window = run[i:i + length]
                occurrences[steps].append(
                    (session, all(e["ok"] for e in window), "".join(e["arg_hash"] for e in window)))
    found = []
    for steps, seen in occurrences.items():
        if not automatable(steps):
            continue
        n = len(seen)
        sessions = len({s for s, _, _ in seen})
        success = sum(ok for _, ok, _ in seen) / n
        gates = (n >= MIN_OCCURRENCES and sessions >= MIN_SESSIONS and success >= MIN_SUCCESS
                 and not any(_DESTRUCTIVE.match(step) for step in steps))
        if not gates:
            continue
        distinct = len({h for _, _, h in seen})
        found.append(Candidate(steps, n, sessions, success, distinct, score=n * sessions * success))
    # A longer sequence that repeats as often as its prefix is the same habit, better described.
    found.sort(key=lambda c: (-c.score, -len(c.steps)))
    kept: list[Candidate] = []
    for c in found:
        if any(_contains(k.steps, c.steps) and k.occurrences >= c.occurrences for k in kept):
            continue
        kept.append(c)
    return kept


def automatable(steps: Sequence[str]) -> bool:
    """Whether a hook could take a sequence over: it starts with an edit or a write
    (what a hook is triggered by) and ends in a command that does something — a
    formatter, a linter, a test run, a build. Anything else is a false positive:
    it repeats, but nothing about it can run on its own or save a turn."""
    if len(steps) < 2 or _EDIT_TRIGGER.match(steps[0]) is None:
        return False
    last = steps[-1]
    return last.startswith("bash:") and last.removeprefix("bash:") not in _LOOKING


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    n = len(shorter)
    return len(longer) > n and any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def recipes(store: Store, kind: str, *, repo: str | None, limit: int = 2) -> list[str]:
    """The sequences that came before passing worker attempts of this kind, most
    common first, with nothing destructive in them."""
    passed = store.passed_attempt_events(kind, repo=repo)
    runs = _runs(passed)
    if len(runs) < MIN_OCCURRENCES:
        return []
    counts: Counter[tuple[str, ...]] = Counter()
    for run in runs.values():
        sigs = [e["sig"] for e in run if not e["sig"].startswith("read:")]
        seen = set()
        for length in (3, 2):
            for i in range(len(sigs) - length + 1):
                steps = tuple(sigs[i:i + length])
                if len(set(steps)) > 1 and steps not in seen and not any(_DESTRUCTIVE.match(s) for s in steps):
                    seen.add(steps)
        counts.update(seen)
    common = [steps for steps, n in counts.most_common() if n >= MIN_OCCURRENCES]
    out: list[str] = []
    for steps in common:
        if any(" → ".join(steps) in r for r in out):
            continue
        out.append(" → ".join(steps))
        if len(out) >= limit:
            break
    return out


# --- automation, only with a person ---------------------------------------------------




class HabitError(ValueError):
    pass


def launcher() -> Path:
    """The absolute path of `bin/cauce`: a hook in settings cannot rely on PATH."""
    return Path(__file__).resolve().parents[2] / "bin" / "cauce"


def install(store: Store, candidate: Candidate, *, command: str, repo_dir: Path) -> tuple[int, bool]:
    """Write the habit as a PostToolUse hook in the repository's local settings.
    Returns its id and whether it is new: the same command on the same file type
    in the same settings is the habit already there, never a second hook.

    Only a habit that starts with an edit can be a hook: the trigger is an edit
    of that file type, and the command is what the person says to run after it.
    """
    match = _EDIT_TRIGGER.match(candidate.steps[0])
    if match is None:
        raise HabitError("only a habit that starts with an edit or a write can run as a hook")
    settings = repo_dir / ".claude" / "settings.local.json"
    for habit in store.habits():
        if (habit["settings_path"] == str(settings) and habit["command"] == command
                and habit["trigger_suffix"] == match.group(2)):
            return int(habit["id"]), False
    data = _read_settings(settings)
    habit_id = store.add_habit(steps=list(candidate.steps), command=command, trigger_suffix=match.group(2),
                               settings_path=str(settings), candidate=candidate.id)
    entry = {"matcher": "Edit|Write|MultiEdit",
             "hooks": [{"type": "command", "command": f'"{launcher()}" habit-run {habit_id}', "timeout": 120}]}
    data.setdefault("hooks", {}).setdefault("PostToolUse", []).append(entry)
    _write_settings(settings, data)
    return habit_id, True


def uninstall(store: Store, habit_id: int) -> bool:
    habit = store.habit(habit_id)
    if habit is None:
        return False
    settings = Path(habit["settings_path"])
    data = _read_settings(settings)
    marker = f"habit-run {habit_id}"
    hooks = data.get("hooks", {}).get("PostToolUse", [])
    data.setdefault("hooks", {})["PostToolUse"] = [
        h for h in hooks if not any(marker in str(x.get("command", "")) for x in h.get("hooks", []))]
    _write_settings(settings, data)
    store.update_habit(habit_id, removed=1)
    return True


#: How much of a failed habit's output the session is shown.
OUTPUT_CHARS = 2000


def run_habit(store: Store, habit_id: int, event: Mapping[str, Any]) -> str | None:
    """The hook body. Runs the person's command after a matching edit; three
    failures in a row and the habit turns itself off. Returns what the session
    should hear: the end of a failed run's output, so the check it ran saves the
    model a turn instead of being run again by hand. None when it passed or did
    not apply."""
    habit = store.habit(habit_id)
    if habit is None or habit["disabled_at"] or habit["removed"]:
        return None
    path = str((event.get("tool_input") or {}).get("file_path") or "")
    if not path.endswith(habit["trigger_suffix"]):
        return None
    try:
        proc = subprocess.run(habit["command"], shell=True, cwd=event.get("cwd") or None,  # noqa: S602
                              capture_output=True, text=True, timeout=110, check=False)
        ok, output = proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, output = False, "it ran past 110 seconds and was stopped"
    except OSError as exc:
        ok, output = False, str(exc)
    store.habit_ran(habit_id, ok=ok, max_failures=MAX_FAILURES)
    if ok:
        return None
    tail = output[-OUTPUT_CHARS:]
    now = store.habit(habit_id)
    off = (" It failed three times in a row and is off now (`cauce habits status` lists it)."
           if now and now["disabled_at"] else "")
    return f"cauce habit #{habit_id} ran `{habit['command']}` after this edit, and it failed:{off}\n{tail}"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HabitError(f"{path} is not valid JSON; fix it before installing a habit") from exc
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
