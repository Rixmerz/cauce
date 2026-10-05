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
}

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


def sync_plugin_options(env: Mapping[str, str]) -> None:
    """Copy `CLAUDE_PLUGIN_OPTION_<KEY>` values into the file, for the commands
    that never see them."""
    updates = {}
    for key in DEFAULTS:
        value = _bool(env.get(f"CLAUDE_PLUGIN_OPTION_{key.upper()}"))
        if value is not None:
            updates[key] = value
    if updates and any(load(env).get(k) != v for k, v in updates.items()):
        save(updates, env)
