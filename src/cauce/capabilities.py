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
- ``touches`` — by what the work touches, whatever its kind: file patterns (or
  ``"ui"`` for rendered UI, `UI_FILES`) matched against the files a task names,
  the files its work changed so far, and the diff a review reads. A capability
  that only ``measures`` (reads, never changes anything) also goes to reading
  work this way: a review, a refuter, a re-check and the independent verifier
  of a change that touches its files. One that can change things joins only
  work that writes.

A worker gets exactly the selected servers (`--strict-mcp-config`), plus a
line per capability saying when to reach for it. Nothing else is in its list,
so nothing competes with it.

The registry lives in the user's cauce home, never in a repository. A server
entry is a command that runs on this machine; a repository that could add one
by committing a file could run anything on the machine of whoever opens it.
"""
from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: What rendered UI is, for a capability that `touches` "ui": the files a page is drawn from.
UI_FILES = ("components/**", "islands/**", "routes/**", "pages/**", "app/**/*.tsx", "src/**/*.tsx",
            "*.css", "*.scss", "*.sass", "*.less", "*.tsx", "*.jsx", "*.vue", "*.svelte", "*.astro", "*.html")


def matches(path: str, patterns: Sequence[str]) -> bool:
    """A path against file patterns, at any depth: `components/**` is a folder of that
    name anywhere, `*.css` a file of that type anywhere."""
    parts = path.split("/")
    for pattern in patterns:
        if pattern.endswith("/**"):
            if pattern[:-3] in ("/".join(parts[i:i + pattern[:-3].count("/") + 1])
                                for i in range(len(parts) - 1)):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


@dataclass(frozen=True)
class Capability:
    name: str
    server: Mapping[str, Any]
    when: str | tuple[str, ...] = "always"
    after_failure: tuple[str, ...] = ()
    hint: str = ""
    #: A folder the worker reads from when it has this capability: the skill of
    #: the plugin that ships it.
    read_dir: str = ""
    #: The files it is about: it joins work that names or changes one, whatever the kind.
    touches: tuple[str, ...] = ()
    #: It only measures, never changes anything: reading work may have it too.
    measures: bool = False

    def touched(self, paths: Sequence[str]) -> str | None:
        """The first of `paths` this capability is about, or None."""
        return next((p for p in paths if self.touches and matches(p, self.touches)), None)

    def applies(self, kind: str, *, failed_before: bool, paths: Sequence[str] = (), reading: bool = False) -> bool:
        if self.when == "always":
            return True
        if isinstance(self.when, tuple) and kind in self.when:
            return True
        if failed_before and kind in self.after_failure:
            return True
        # By what the work touches: reading work only gets one that measures.
        return (self.measures or not reading) and self.touched(paths) is not None


@dataclass(frozen=True)
class Selection:
    servers: Mapping[str, Any] = field(default_factory=dict)
    hints: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    read_dirs: tuple[str, ...] = ()

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
        touches = entry.get("touches", [])
        if touches == "ui":
            touches = list(UI_FILES)
        elif not isinstance(touches, list):
            raise RegistryError(f"capability {name!r}: `touches` is \"ui\" or a list of file patterns")
        registry[name] = Capability(
            name=name,
            server=entry["server"],
            when=when,
            after_failure=tuple(str(k) for k in entry.get("after_failure", [])),
            hint=str(entry.get("hint", "")),
            touches=tuple(str(t) for t in touches),
            measures=bool(entry.get("measures", False)),
        )
    return registry


def load_all(path: Path, env: Mapping[str, str] | None = None) -> dict[str, Capability]:
    """The registry at `path` over the neighbours the person installed as Claude
    Code plugins (`cauce.plugins`): a registry entry of the same name wins, and
    the `plugins` setting off leaves the installed ones out."""
    import os

    from cauce import config, plugins

    env = os.environ if env is None else env
    found = plugins.capabilities(env) if config.enabled("plugins", env) else {}
    return {**found, **load(path)}


def select(
    registry: Mapping[str, Capability],
    kind: str,
    *,
    failed_before: bool = False,
    workdir: Path | None = None,
    every: bool = False,
    paths: Sequence[str] = (),
    reading: bool = False,
) -> Selection:
    """The capabilities a task of `kind` gets; `every` takes all of `registry`,
    for the ones a worker's role names whatever their `when`. `paths` are the files
    the work names or changed (a review's: the diff it reads); `reading` work gets
    by them only what measures."""
    chosen = [c for c in registry.values()
              if every or c.applies(kind, failed_before=failed_before, paths=paths, reading=reading)]
    values = {"workdir": str(workdir) if workdir else ""}
    servers = {c.name: _fill(c.server, values) for c in chosen}
    hints = tuple(f"{c.name}: {_fill(c.hint, values)}" + _why_touched(c, kind, paths) for c in chosen if c.hint)
    return Selection(servers, hints, tuple(c.name for c in chosen), tuple(c.read_dir for c in chosen if c.read_dir))


def _why_touched(c: Capability, kind: str, paths: Sequence[str]) -> str:
    """Said when a capability came by what the work touches, not by its kind."""
    if c.when == "always" or (isinstance(c.when, tuple) and kind in c.when):
        return ""
    hit = c.touched(paths)
    return f" (given because this work touches {hit})" if hit else ""


def measuring(registry: Mapping[str, Capability]) -> dict[str, Capability]:
    """The capabilities that only measure: the read-only independent check may have them."""
    return {name: c for name, c in registry.items() if c.measures}


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
        "touches": "ui",
        "measures": True,
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
