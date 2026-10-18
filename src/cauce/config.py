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
    # engram (the team's memory, when its server runs): searched into the brief,
    # and handed to workers read-only. See `cauce.adapters.engram`.
    "engram": True,
    # What a passing worker learned is also saved in engram, for the team. Off
    # unless the person turns it on: it writes to a store that is not cauce's.
    "engram_write": False,
    # When a session should compact, and what `/compact` should keep (`cauce.compaction`):
    # `suggest` shows the person the line at a good moment, `auto` has the mod submit
    # it (only on a Claude Code build seen to run it), `off` leaves it alone.
    "compact": "suggest",
    # The mod turns Claude Code's `ask` for `queue` and `resume` into an allow: the
    # person delegated starting cauce work to the session. A deny stays a deny, and a
    # resume still runs `--unattended`. Off: each call is the person's.
    "delegate": True,
}

#: Settings that take one of a few words rather than on or off.
CHOICES: dict[str, tuple[str, ...]] = {"compact": ("suggest", "auto", "off")}

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


def sync_plugin_options(env: Mapping[str, str]) -> None:
    """Copy `CLAUDE_PLUGIN_OPTION_<KEY>` values into the file, for the commands
    that never see them."""
    updates: dict[str, Any] = {}
    for key in DEFAULTS:
        raw = env.get(f"CLAUDE_PLUGIN_OPTION_{key.upper()}")
        value = word(key, raw) if key in CHOICES else _bool(raw)
        if value is not None:
            updates[key] = value
    if updates and any(load(env).get(k) != v for k, v in updates.items()):
        save(updates, env)
