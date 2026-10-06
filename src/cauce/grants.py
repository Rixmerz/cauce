"""What workers may do: the permission mode they run in, and the rules a person
grants the workers of one repository, once.

**Modes.** A one-shot worker has nobody to approve a command, so under
`acceptEdits` every command its settings do not name is refused, and a
worker that reaches for `node` by another path meets a new refusal each
time. Workers run in Claude Code's `auto` mode instead, where a classifier
lets through what is safe and refuses what is not, with no rule per command.
Haiku is not served by auto mode, so Haiku workers run with
`bypassPermissions`: no check at all. Deny rules still hold under it (a
reading task's write tools stay removed), but Bash can do anything. Both are
settings a person changes: `cauce config mode <model|default> <mode>`.

`--allow` grants a rule to one run, and a resume keeps it. A new task in the
same repository started with none of them, so every task met the same
refusals again and a person passed the same rules again. `cauce allow` keeps
rules per repository, and every run and resume there gets them.

They live in the person's own cauce home (`config.json`), keyed by the
repository, never in the repository: a repository must not be able to grant
its own workers anything by committing a file. A person grants them by
running `cauce allow`; cauce only suggests (see `cauce.allow`).

Presets name the usual sets so nobody types ten rules:

- `read`: commands that only read, for audits and reviews;
- `node`, `python`: a project's own tooling, run by name.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from cauce import config

#: The mode a worker runs in, per model alias, and for every other model.
DEFAULT_MODES = {"default": "auto", "haiku": "bypassPermissions"}
#: What a worker may be set to. `plan` is left out: it stops to wait for a person.
MODES = ("auto", "acceptEdits", "bypassPermissions", "dontAsk", "manual")
#: What a worker falls back to when the CLI does not take its mode.
FALLBACK_MODE = "acceptEdits"


def mode_for(alias: str, env: Mapping[str, str] | None = None) -> str:
    """The permission mode a worker of this model runs in."""
    env = os.environ if env is None else env
    chosen = dict(DEFAULT_MODES)
    chosen.update({k: v for k, v in (config.load(env).get("modes") or {}).items() if v in MODES})
    for key in (f"CAUCE_MODE_{alias.upper()}", "CAUCE_MODE"):
        if env.get(key) in MODES:
            return env[key]
    return chosen.get(alias) or chosen["default"]


def set_mode(alias: str, mode: str | None, env: Mapping[str, str] | None = None) -> None:
    """Set a model's mode (`default` for every other model); None puts back cauce's own."""
    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; known: {', '.join(MODES)}")
    current = dict(config.load(env).get("modes") or {})
    if mode is None:
        current.pop(alias, None)
    else:
        current[alias] = mode
    config.save({"modes": current}, env)


PRESETS: dict[str, tuple[str, ...]] = {
    "read": ("Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)",
             "Bash(rg:*)", "Bash(tree:*)", "Bash(pwd)", "Bash(git status:*)", "Bash(git log:*)",
             "Bash(git diff:*)", "Bash(git show:*)", "Bash(git branch:*)"),
    "node": ("Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)"),
    "python": ("Bash(python:*)", "Bash(python3:*)", "Bash(pytest:*)", "Bash(uv:*)", "Bash(pip:*)"),
}


def granted(repo: str | None, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The rules a person granted every worker in `repo`."""
    if not repo:
        return ()
    stored = (config.load(env).get("grants") or {}).get(repo) or []
    return tuple(str(rule) for rule in stored if isinstance(rule, str) and rule.strip())


def expand(rules: Iterable[str], presets: Iterable[str] = ()) -> tuple[str, ...]:
    out: list[str] = []
    for name in presets:
        if name not in PRESETS:
            raise ValueError(f"unknown preset {name!r}; known: {', '.join(PRESETS)}")
        out += PRESETS[name]
    out += [r.strip() for r in rules if r.strip()]
    return tuple(dict.fromkeys(out))


def grant(repo: str, rules: Iterable[str], env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Add rules for a repository; returns every rule it now has."""
    return _save(repo, (*granted(repo, env), *rules), env)


def revoke(repo: str, rules: Iterable[str], env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Take rules back; with none named, take back all of them."""
    gone = set(rules)
    return _save(repo, [r for r in granted(repo, env) if gone and r not in gone], env)


def _save(repo: str, rules: Iterable[str], env: Mapping[str, str] | None) -> tuple[str, ...]:
    kept = tuple(dict.fromkeys(rules))
    every = dict(config.load(env).get("grants") or {})
    if kept:
        every[repo] = list(kept)
    else:
        every.pop(repo, None)
    config.save({"grants": every}, env)
    return kept
