"""Which capabilities a worker is handed, decided by the core, not remembered by the model.

The problem this exists for: in an ordinary Claude Code session, a plugin's
tools sit in a list of dozens and get called when the model happens to think of
them, which is rarely. A capability that is always offered and seldom used is
the same as one that is not installed.

So the core decides. Each capability declares when it belongs:

- ``"always"`` — transversal. A code index belongs in every worker that reads
  or writes code.
- a list of kinds — it belongs to that work: a layout inspector in UI tasks.
- ``after_failure`` — reactive. It joins the *next* attempt once one of these
  kinds has failed: the layout inspector after a UI attempt the checks rejected.

A worker gets exactly the selected servers (`--strict-mcp-config`), plus a
line per capability saying when to reach for it. Nothing else is in its list,
so nothing competes with it.

The registry lives in the user's cauce home, never in a repository. A server
entry is a command that runs on this machine; a repository that could add one
by committing a file could run anything on the machine of whoever opens it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    server: Mapping[str, Any]
    when: str | tuple[str, ...] = "always"
    after_failure: tuple[str, ...] = ()
    hint: str = ""

    def applies(self, kind: str, *, failed_before: bool) -> bool:
        if self.when == "always":
            return True
        if isinstance(self.when, tuple) and kind in self.when:
            return True
        return failed_before and kind in self.after_failure


@dataclass(frozen=True)
class Selection:
    servers: Mapping[str, Any] = field(default_factory=dict)
    hints: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    def system_prompt(self) -> str:
        if not self.hints:
            return ""
        return "Capabilities this task was given, and when to use them:\n" + "\n".join(
            f"- {h}" for h in self.hints
        )


class RegistryError(ValueError):
    pass


def load(path: Path) -> dict[str, Capability]:
    """The registry at `path`; an absent file is an empty registry."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RegistryError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must hold an object of capabilities")
    registry: dict[str, Capability] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("server"), dict):
            raise RegistryError(f"capability {name!r} needs a `server` object")
        when = entry.get("when", "always")
        if isinstance(when, list):
            when = tuple(str(k) for k in when)
        elif when != "always":
            raise RegistryError(f"capability {name!r}: `when` is \"always\" or a list of kinds")
        registry[name] = Capability(
            name=name,
            server=entry["server"],
            when=when,
            after_failure=tuple(str(k) for k in entry.get("after_failure", [])),
            hint=str(entry.get("hint", "")),
        )
    return registry


def select(
    registry: Mapping[str, Capability],
    kind: str,
    *,
    failed_before: bool = False,
    workdir: Path | None = None,
) -> Selection:
    chosen = [c for c in registry.values() if c.applies(kind, failed_before=failed_before)]
    values = {"workdir": str(workdir) if workdir else ""}
    servers = {c.name: _fill(c.server, values) for c in chosen}
    hints = tuple(f"{c.name}: {_fill(c.hint, values)}" for c in chosen if c.hint)
    return Selection(servers, hints, tuple(c.name for c in chosen))


def _fill(value: Any, values: Mapping[str, str]) -> Any:
    """`{workdir}` in a server's args, env or hint becomes the task's directory:
    a code index that takes the workspace as an argument gets the right one."""
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, Mapping):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_fill(v, values) for v in value]
    return value


EXAMPLE = {
    "livespec": {
        "server": {"command": "livespec", "args": []},
        "when": "always",
        "hint": "before editing a symbol, ask who calls it (who_calls) and what breaks "
        "(analyze_impact); pass workspace=\"{workdir}\" on every call",
    },
    "layout-inspector": {
        "server": {"command": "layout-inspector", "args": []},
        "when": ["ui"],
        "after_failure": ["implement", "feature"],
        "hint": "measure the rendered page (detect_issues, compare_viewports) instead of "
        "judging it by eye",
    },
    "browser": {
        "server": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        "when": ["test"],
        "after_failure": ["ui"],
        "hint": "open the running app with browser_navigate and read it with browser_snapshot; report what "
        "the page shows, never what it should show",
    },
}
