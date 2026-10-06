"""Which model each alias runs as, and whether workers run an older one than the
person's own sessions.

A worker is started with an alias (`--model sonnet`) and the installed Claude
Code decides which model that is. That keeps cauce off any provider's model
names, and follows Claude Code as it updates, but an alias can lag: a
person's sessions may already run a newer Sonnet while `sonnet` still names
the older one in the CLI the workers use. So:

- a person can pin an alias to a model id (`cauce config model sonnet <id>`, or
  `CAUCE_MODEL_SONNET`); unpinned, the alias goes through as it is;
- every attempt records the model that actually served it, read from the
  CLI's own `modelUsage`, never assumed from the alias;
- when the newest model a person's sessions used is a newer version of a
  family than the one that last served its workers, the plan says so, with
  the command that pins it.

Light: config and the store only.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from cauce import config
from cauce.matrix import MODELS

#: What a pin may hold: a model id, as Claude Code or a provider spells it.
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")


def pinned(alias: str, env: Mapping[str, str] | None = None) -> str | None:
    """The model id a person pinned an alias to, or None to let the CLI decide."""
    env = os.environ if env is None else env
    for value in (env.get(f"CAUCE_MODEL_{alias.upper()}"), (config.load(env).get("models") or {}).get(alias)):
        if value and MODEL_ID.match(str(value)):
            return str(value)
    return None  # an id that is not one is ignored, never passed to the CLI


def resolve(alias: str, env: Mapping[str, str] | None = None) -> str:
    """What `--model` gets for an alias."""
    return pinned(alias, env) or alias


def pin(alias: str, model_id: str | None, env: Mapping[str, str] | None = None) -> None:
    """Pin an alias to a model id; None unpins it."""
    if alias not in MODELS:
        raise ValueError(f"unknown model {alias!r}; known: {', '.join(MODELS)}")
    if model_id is not None and not MODEL_ID.match(model_id):
        raise ValueError(f"{model_id!r} does not look like a model id")
    current = dict(config.load(env).get("models") or {})
    if model_id is None:
        current.pop(alias, None)
    else:
        current[alias] = model_id
    config.save({"models": current}, env)


def family(model_id: str | None) -> str | None:
    name = (model_id or "").lower()
    return next((alias for alias in MODELS if alias in name), None)


def version(model_id: str | None) -> tuple[int, ...] | None:
    """`claude-sonnet-5-5` → (5, 5); `claude-sonnet-4-5-20250929` → (4, 5);
    `claude-3-5-sonnet-20241022` → (3, 5). None when there is no number."""
    name = (model_id or "").lower()
    alias = family(name)
    if alias is None:
        return None
    for found in (re.search(rf"{alias}-(\d+(?:-\d+)*)", name), re.search(rf"(\d+(?:-\d+)*)-{alias}", name)):
        parts = tuple(int(p) for p in found.group(1).split("-") if len(p) < 8) if found else ()  # a date is no version
        if parts:
            return parts
    return None


def served(envelope: Mapping[str, Any] | None) -> str:
    """The model that did most of an attempt's work, from the CLI's `modelUsage`:
    a worker's own small helper calls do not count as the model that served it."""
    usage = (envelope or {}).get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return ""
    def output(item: tuple[str, Any]) -> int:
        stats = item[1] if isinstance(item[1], dict) else {}
        value = stats.get("outputTokens")
        return int(value) if isinstance(value, (int, float)) else 0
    return str(max(usage.items(), key=output)[0])


def lagging(store: Any, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Per family, where the workers' model is an older version than the newest the
    person's sessions used. Pinned aliases are left out: a person chose them."""
    newest_session: dict[str, tuple[tuple[int, ...], str]] = {}
    for model_id in store.session_models():
        alias, ver = family(model_id), version(model_id)
        if alias and ver and (alias not in newest_session or ver > newest_session[alias][0]):
            newest_session[alias] = (ver, model_id)
    out = []
    for alias, worker_id in store.worker_models().items():
        ver = version(worker_id)
        if alias not in newest_session or ver is None or pinned(alias, env):
            continue
        session_ver, session_id = newest_session[alias]
        if session_ver > ver:
            out.append({"alias": alias, "workers": worker_id, "sessions": session_id,
                        "command": f"cauce config model {alias} {session_id}"})
    return out
