"""The plugin's files point at things that exist."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cauce import __version__, hooks

ROOT = Path(__file__).resolve().parents[1]


def test_every_hook_registered_is_one_cauce_handles():
    registered = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(registered) == {*hooks.WITH_ENV, *hooks.HANDLERS}
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


def test_the_mod_is_declared_beside_the_classic_hooks():
    hooks_json = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    for module in hooks_json["modules"]:
        assert (ROOT / "hooks" / module).is_file()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert (ROOT / plugin["types"]).is_file()
    assert plugin["userConfig"]["wake"]["default"] is True
    # the classic hooks stay: without the mod, endings and notes still reach the session
    assert {"SessionStart", "UserPromptSubmit", "Stop", "PreCompact"} <= set(hooks_json["hooks"])
