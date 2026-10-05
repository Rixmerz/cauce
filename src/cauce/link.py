"""`cauce link`: the `cauce` command in a terminal, outside Claude Code.

Claude Code installs the plugin under a directory named for its version, so a
symlink to it breaks at the next update. What is written instead is a small
shim that, each time it runs, finds the newest cauce Claude Code has installed.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

MARKER = "# Written by `cauce link`."
SHIM = """#!/usr/bin/env bash
{marker} It runs the newest cauce Claude Code has installed, so a plugin
# update never breaks it. `cauce link --remove` takes it away.
set -euo pipefail
CONFIG="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
newest=""
for c in "$CONFIG"/plugins/cache/*/cauce/*/bin/cauce; do
  [ -x "$c" ] || continue
  if [ -z "$newest" ] || [ "$c" -nt "$newest" ]; then newest="$c"; fi
done
if [ -z "$newest" ] && [ -x {fallback} ]; then newest={fallback}; fi
if [ -z "$newest" ]; then
  echo "cauce: no installed cauce found; claude plugin install cauce@rixmerz" >&2
  exit 127
fi
exec "$newest" "$@"
"""


def default_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home()) / ".local" / "bin"


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def write(target_dir: Path, launcher: Path) -> Path:
    """Write the shim; refuses to replace a `cauce` that this command did not write."""
    target = target_dir / "cauce"
    if target.is_symlink() or (target.exists() and MARKER not in target.read_text(errors="replace")):
        raise FileExistsError(f"{target} exists and was not written by `cauce link`; remove it first")
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(SHIM.format(marker=MARKER, fallback=_quote(launcher)))
    target.chmod(0o755)
    return target


def remove(target_dir: Path) -> Path | None:
    target = target_dir / "cauce"
    if target.is_file() and not target.is_symlink() and MARKER in target.read_text(errors="replace"):
        target.unlink()
        return target
    return None


def on_path(target_dir: Path, env: Mapping[str, str]) -> bool:
    entries = [Path(p).expanduser() for p in env.get("PATH", "").split(os.pathsep) if p]
    return target_dir.resolve() in {e.resolve() for e in entries}
