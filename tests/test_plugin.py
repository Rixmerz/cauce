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


def test_every_setting_is_an_option_on_the_plugin_s_settings_screen():
    from cauce import config

    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert set(config.DEFAULTS) <= set(plugin["userConfig"])
    for key, default in config.DEFAULTS.items():
        option = plugin["userConfig"][key]["default"]
        assert type(option) is type(default) and option == default
        if key in config.CHOICES:
            assert plugin["userConfig"][key]["options"] == list(config.CHOICES[key])


def test_no_guidance_tells_a_session_to_pass_a_budget():
    """Cost is measured, never a brake: guidance that runs in other people's
    repositories never hands a session a budget flag that no command takes."""
    root = Path(__file__).resolve().parent.parent
    texts = [*root.glob("commands/*.md"), *root.glob("skills/*/SKILL.md"), *root.glob("hooks/*.tsx"),
             *root.glob("src/cauce/roles*/*.md"), *root.glob("src/cauce/guides/*.md")]
    assert texts and not [p.name for p in texts if "--budget" in p.read_text(encoding="utf-8")]


def test_the_skill_is_short_and_every_guide_it_names_exists(capsys):
    from cauce import cli

    command = (ROOT / "commands" / "orchestration.md").read_text()
    assert len(command.splitlines()) < 120
    named = {"when", "dispatch", "endings", "orchestrator", "workflows", "notes", "memory", "relay"}
    shipped = {p.stem for p in (ROOT / "src" / "cauce" / "guides").glob("*.md")}
    assert named == shipped and all(f"`{n}`" in command for n in named)
    assert cli.main(["guide"]) == 0
    assert "endings" in capsys.readouterr().out
    assert cli.main(["guide", "endings"]) == 0
    assert "`replan` with `split`" in capsys.readouterr().out
    assert cli.main(["guide", "nope"]) == 2
