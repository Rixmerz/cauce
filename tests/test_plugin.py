"""The plugin's files point at things that exist."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cauce import __version__, hooks

ROOT = Path(__file__).resolve().parents[1]


def test_every_hook_registered_is_one_cauce_handles():
    registered = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(registered) == {"SessionStart", *hooks.HANDLERS}
    for event, entries in registered.items():
        command = entries[0]["hooks"][0]["command"]
        assert command.endswith(f"hook {event}") and "${CLAUDE_PLUGIN_ROOT}/bin/cauce" in command


def test_versions_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert plugin["version"] == market["plugins"][0]["version"] == __version__
    assert f'version = "{__version__}"' in pyproject


def test_the_launcher_is_executable_and_the_entry_command_uses_it():
    assert os.access(ROOT / "bin" / "cauce", os.X_OK)
    command = (ROOT / "commands" / "orchestration.md").read_text()
    assert "cauce run" in command and "$ARGUMENTS" in command
