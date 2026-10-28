"""What a neighbour says of itself: the `cauce` block of its installed plugin manifest.

A neighbour (livespec, layout-inspector, rastro) knows its own tools better than cauce does, and
it changes them more often than cauce is released. So a plugin may publish, in its installed
manifest (`<plugin cache>/<marketplace>/<name>/<version>/.claude-plugin/plugin.json`) under the key
`cauce`, or in a `cauce.json` next to that file, what a worker should be told of it:

    {"format": 1,
     "tools": {"detect_issues": "what it is for", ...},
     "hint": "the line a worker gets",
     "hints": {"implement": "the line for this kind of work", ...},
     "measures": {"detect_issues": "what it measures", ...}}

Only words come from there, never policy. What a manifest can change is the text a worker or the
independent check reads: the hint, the hint per kind of work, the tools' descriptions and what
each tool measures. Which servers a worker gets, when, with which arguments, behind which guard,
which tools a guard lets through and who the verifier gets stay in cauce's code, whatever a
manifest claims (`docs/neighbours.md`).

The manifest is read for the newest installed version only (`plugins.versions`, which never
reads a half-done `temp_*` clone), from the person's plugin cache, never from a repository. It
is read when a task is planned, so an updated plugin is heard on the next task, and kept per
(file, version, mtime) so a plan does not parse it twice. A manifest that is not JSON, names
another `format`, or holds a field of the wrong type is ignored whole: the built-in text is used,
never part of one and part of the other. A hint that names a tool the manifest's own `tools`
lacks is dropped, so every tool a hint names is still one the neighbour lists.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1
KEY = "cauce"
FILE = "cauce.json"
#: What the plan says, once a day per repository, when some neighbour's words came from its manifest.
PREFIX = "manifests: "

#: A tool a hint names: a snake_case word (`detect_issues`) or a word in backticks (`search`).
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_TICKED = re.compile(r"`([A-Za-z0-9_]+)`")


@dataclass(frozen=True)
class Manifest:
    plugin: str
    version: str
    tools: Mapping[str, str] = field(default_factory=dict)
    hint: str = ""
    hints: Mapping[str, str] = field(default_factory=dict)
    measures: Mapping[str, str] = field(default_factory=dict)

    def hint_for(self, kind: str) -> str:
        """The line for this kind of work, else the general one; empty when it has neither."""
        return self.hints.get(kind) or self.hint


def named_tools(text: str) -> set[str]:
    return set(_SNAKE.findall(text)) | set(_TICKED.findall(text))


def _texts(value: Any) -> dict[str, str] | None:
    """A `{name: text}` object, or None when it is anything else."""
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return None
    return {k: v.strip() for k, v in value.items() if k.strip() and v.strip()}


def parse(data: Any, plugin: str, version: str) -> Manifest | None:
    """The block as a Manifest, or None when any of it is not what the format says."""
    if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != FORMAT:
        return None
    tools = _texts(data.get("tools", {}))
    hints = _texts(data.get("hints", {}))
    measures = _texts(data.get("measures", {}))
    hint = data.get("hint", "")
    if tools is None or hints is None or measures is None or not isinstance(hint, str):
        return None

    def kept(text: str) -> bool:
        return named_tools(text) <= tools.keys()

    return Manifest(plugin, version, tools, hint.strip() if kept(hint) else "",
                    {k: v for k, v in hints.items() if kept(v)},
                    {k: v for k, v in measures.items() if k in tools})


_CACHE: dict[tuple[str, str, tuple[int, ...]], Manifest | None] = {}


def _block(root: Path) -> tuple[Path, Any] | None:
    """The manifest's `cauce` block, else the whole of `cauce.json` beside it."""
    folder = root / ".claude-plugin"
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and KEY in manifest:
            return folder / "plugin.json", manifest[KEY]
    path = folder / FILE
    with contextlib.suppress(OSError, ValueError):
        return path, json.loads(path.read_text(encoding="utf-8"))
    return None


def read(plugin: str, env: Mapping[str, str] | None = None) -> Manifest | None:
    """The newest installed version's block, or None: no plugin, no block, or one not to trust."""
    from cauce import plugins

    env = os.environ if env is None else env
    roots = plugins.versions(env, plugin)
    if not roots:
        return None
    root = roots[0]
    stamp = []
    for name in ("plugin.json", FILE):
        with contextlib.suppress(OSError):
            stamp.append((root / ".claude-plugin" / name).stat().st_mtime_ns)
    key = (str(root), root.name, tuple(stamp))
    if key not in _CACHE:
        found = _block(root)
        _CACHE[key] = parse(found[1], plugin, root.name) if found else None
    return _CACHE[key]


def read_all(env: Mapping[str, str] | None = None) -> dict[str, Manifest | None]:
    """Every neighbour cauce knows (livespec and `plugins.KNOWN`), each with its manifest or None."""
    from cauce import plugins

    return {name: read(name, env) for name in ("livespec", *(n.plugin for n in plugins.KNOWN))}


def plan_line(plugins_read: Mapping[str, Manifest | None]) -> str | None:
    """Whose words came from their own manifest, and which release, said in the plan."""
    used = sorted(f"{m.plugin} {m.version}" for m in plugins_read.values() if m is not None)
    if not used:
        return None
    return (f"{PREFIX}{', '.join(used)} told cauce how to use {'it' if len(used) == 1 else 'them'} "
            "(their plugin manifest); policy and every other neighbour stay cauce's own")
