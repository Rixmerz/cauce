"""cauce's own settings, in `$CAUCE_HOME/config.json`.

A plugin option reaches hooks and MCP servers as `CLAUDE_PLUGIN_OPTION_<KEY>`,
but not the Bash commands the model runs — and `cauce run` is one of those. So
the SessionStart hook copies the options it is given into this file, and every
entry point reads the file. The plugin's settings screen stays the one place a
person changes them.

Precedence, highest first: an explicit flag, a `CAUCE_<KEY>` variable, this
file, the default.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cauce.store import home

DEFAULTS: dict[str, Any] = {
    # livespec is part of the base: on unless someone turns it off.
    "livespec": True,
    # Haiku decides whether a queued task can start beside the ones running in
    # its repository. Off: one task at a time per repository.
    "parallel": True,
    # A task queued with `++` starts a dispatcher for its repository at once.
    # Off: queued work waits for `cauce work`.
    "autowork": True,
    # Haiku names each session of an enrolled project in `.cauce/sessions.json`.
    "names": True,
    # Each project keeps notes of what it knows, by topic: workers and sessions
    # add to them (Haiku files them), workers and sessions read them back.
    "notes": True,
    # The scribe keeps each session's facts as it goes (requests verbatim, files,
    # failed calls), from its transcript and without a model: work the session sends
    # gets its thread, and the notes are filed every few turns. See `cauce.scribe`.
    "scribe": True,
    # A branch cauce kept is deleted once its work landed: merged, squash-merged,
    # or holding only tool artifacts. Its tip stays on the task's events.
    "prune": True,
    # Workers get the MCP servers of the neighbours the person installed as Claude
    # Code plugins (layout-inspector, rastro): see `cauce.plugins`.
    "plugins": True,
    # A passing change whose risk is high (or medium, made by a cheap cell) is
    # checked again by an independent verifier before it counts.
    "verifier": True,
    # A start that rarely passes its first try here races the next rung on that try, each in
    # its own worktree; the first that passes its check is kept (`orchestrate.race_cell`).
    "race": True,
    # Each request is advised the way it is best done: by the session itself, one worker,
    # or in parts side by side (`cauce.route`); the session decides.
    "route": True,
    # A one-shot worker gets no subagent tool (whose description lists every agent the
    # repository and the person define), no skill listing (its role's skills are in its
    # brief), and its per-machine prompt sections after the shared part, for the cache.
    # Only the flags the installed CLI lists in its `--help` (`launch.lean_flags`).
    "lean": True,
    "primer": True,
    # A workflow step that names a reading step before it as its `trunk` starts as a fork of
    # that step's session, its role given as its first message there (`cauce.trunks`); each
    # repository alternates forked and fresh first tries and keeps forking where it won.
    "trunk": True,
    # The independent check starts beside cauce's own check, not after it: it reads the same
    # diff, a failed check stops it, and it is told not to run the check itself (`orchestrate.Ahead`).
    "overlap": True,
    # The independent check of a task that is no workflow step and that nothing waits on runs
    # after its ending: the run ends on cauce's own check, and a blocker the verifier shows
    # reopens it as `review`. Off: the ending waits for the verifier.
    "late_verifier": True,
    # A repository that keeps OpenSpec specs (`openspec/`): a writing worker is told to keep the
    # ones its change reaches, and an ending whose change touched none, where the specs are behind
    # the code, offers the task that catches them up (`cauce.specs`). Off: specs are only read.
    "specs": True,
    # A repository that follows TRACER (`docs/tracer/`): its writing workers read the audience,
    # floors, anti-patterns, identity and the flows their request names, and the independent check
    # holds the change to those flows' CORRECT lines and the catalogue (`cauce.tracer`).
    "tracer": True,
    # A role's lessons in each repository improve by measured trial: an editor (Haiku) proposes
    # them from what the role's workers met there, its tasks alternate with and without them,
    # and they are kept only where they won (`cauce.revisions`). Off: the role's own text alone.
    "role_trials": True,
    # A habit newly found in a project is reviewed by a read-only worker (its tool here, a
    # command scoped to the edited file, timed): its ending lists the install line for the
    # orchestrator to put to the person. Installing stays theirs (`cauce habits install`).
    "habit_review": True,
    # A review's habits that pass cauce's own check (one simple command running the habit's
    # program, passing on clean code at HEAD, fast, writing nothing) are installed by cauce
    # itself, said in the ending with the line that removes them. Off: the person installs.
    "habit_auto": True,
    # engram (the team's memory, when its server runs): searched into the brief,
    # and handed to workers read-only. See `cauce.adapters.engram`.
    "engram": True,
    # What a passing worker learned is also saved in engram, for the team. Off
    # unless the person turns it on: it writes to a store that is not cauce's.
    "engram_write": False,
    # When a session should compact, and what `/compact` should keep (`cauce.compaction`):
    # `orchestrator` tells the session at a good moment and it decides, compacting with
    # the mod's `compact` tool; `suggest` shows the person the line, `auto` has the mod
    # submit it (only on a Claude Code build seen to run it), `off` leaves it alone.
    "compact": "orchestrator",
    # The mod turns Claude Code's `ask` for `queue` and `resume` into an allow: the
    # person delegated starting cauce work to the session. A deny stays a deny, and a
    # resume still runs `--unattended`. Off: each call is the person's.
    "delegate": True,
    # The most workers this machine runs at once (`cauce.capacity`): `auto` puts no
    # ceiling over the level the dispatcher adapts to the API and the machine; a number
    # is the person's ceiling, and the level still adapts under it.
    "max_parallel": "auto",
    # The first task in a repository livespec has not indexed waits up to this many seconds for
    # the index when the repository is small enough to build one that fast (`cauce.adapters.
    # livespec`); a larger one indexes in the background and that task runs without it. `0`
    # never waits.
    "index_wait": "20",
}

#: Settings that take one of a few words rather than on or off.
CHOICES: dict[str, tuple[str, ...]] = {"compact": ("orchestrator", "suggest", "auto", "off")}

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


def _path(env: Mapping[str, str] | None) -> Path:
    return home(dict(env) if env is not None else None) / "config.json"


def load(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        stored = json.loads(_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return {**DEFAULTS, **(stored if isinstance(stored, dict) else {})}


def save(values: Mapping[str, Any], env: Mapping[str, str] | None = None) -> None:
    path = _path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load(env)
    current.update(values)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    return True if value in _TRUE else False if value in _FALSE else None


def enabled(key: str, env: Mapping[str, str], flag: bool | None = None) -> bool:
    if flag is not None:
        return flag
    from_env = _bool(env.get(f"CAUCE_{key.upper()}"))
    if from_env is not None:
        return from_env
    return bool(load(env).get(key, DEFAULTS.get(key, False)))


def word(key: str, raw: Any) -> str | None:
    """`raw` as one of the words setting `key` takes, or None."""
    value = str(raw).strip().lower() if isinstance(raw, str) else None
    return value if value in CHOICES[key] else None


def choice(key: str, env: Mapping[str, str]) -> str:
    """A setting that takes a word: the `CAUCE_<KEY>` variable, else the file, else
    the default. A word the setting does not take is read as unset."""
    return word(key, env.get(f"CAUCE_{key.upper()}")) or word(key, load(env).get(key)) or DEFAULTS[key]


def ceiling_word(raw: Any) -> str | None:
    """`raw` as `max_parallel` takes it: `auto`, or a whole number from 1 as text; None otherwise."""
    value = str(raw).strip().lower() if isinstance(raw, str | int) and not isinstance(raw, bool) else ""
    return value if value == "auto" or (value.isdigit() and int(value) >= 1) else None


def max_parallel(env: Mapping[str, str]) -> int | None:
    """The person's ceiling on the workers this machine runs at once; None for `auto`. The
    `CAUCE_MAX_PARALLEL` variable, else the file; a value it does not take reads as unset."""
    picked = ceiling_word(env.get("CAUCE_MAX_PARALLEL")) or ceiling_word(load(env).get("max_parallel")) or "auto"
    return None if picked == "auto" else int(picked)


def seconds_word(raw: Any) -> str | None:
    """`raw` as `index_wait` takes it: whole seconds from 0 as text; None otherwise."""
    value = str(raw).strip() if isinstance(raw, str | int) and not isinstance(raw, bool) else ""
    return value if value.isdigit() else None


def index_wait(env: Mapping[str, str]) -> int:
    """Seconds the first task of an unindexed repository waits for livespec's index: the
    `CAUCE_INDEX_WAIT` variable, else the file; a value it does not take reads as unset."""
    return int(seconds_word(env.get("CAUCE_INDEX_WAIT")) or seconds_word(load(env).get("index_wait"))
               or DEFAULTS["index_wait"])


def sync_plugin_options(env: Mapping[str, str]) -> None:
    """Copy `CLAUDE_PLUGIN_OPTION_<KEY>` values into the file, for the commands
    that never see them."""
    updates: dict[str, Any] = {}
    for key in DEFAULTS:
        raw = env.get(f"CLAUDE_PLUGIN_OPTION_{key.upper()}")
        value = (ceiling_word(raw) if key == "max_parallel" else seconds_word(raw) if key == "index_wait"
                 else word(key, raw) if key in CHOICES else _bool(raw))
        if value is not None:
            updates[key] = value
    if updates and any(load(env).get(k) != v for k, v in updates.items()):
        save(updates, env)
