from __future__ import annotations

from cauce import config


def test_defaults_file_env_and_flag(_isolated_home):
    env = {"CAUCE_HOME": str(_isolated_home)}
    assert config.load(env)["livespec"] is True
    assert config.enabled("livespec", env)
    config.save({"livespec": False}, env)
    assert not config.enabled("livespec", env)
    assert config.enabled("livespec", {**env, "CAUCE_LIVESPEC": "on"})
    assert not config.enabled("livespec", {**env, "CAUCE_LIVESPEC": "on"}, flag=False)
    assert not config.enabled("livespec", {**env, "CAUCE_LIVESPEC": "maybe"})  # unparseable env is ignored


def test_plugin_options_reach_the_file(_isolated_home):
    env = {"CAUCE_HOME": str(_isolated_home), "CLAUDE_PLUGIN_OPTION_LIVESPEC": "false"}
    config.sync_plugin_options(env)
    assert config.load(env)["livespec"] is False
    config.sync_plugin_options({"CAUCE_HOME": str(_isolated_home), "CLAUDE_PLUGIN_OPTION_LIVESPEC": "true"})
    assert config.load(env)["livespec"] is True
    config.sync_plugin_options({"CAUCE_HOME": str(_isolated_home)})  # no option: nothing changes
    assert config.load(env)["livespec"] is True


def test_a_broken_file_is_the_defaults(_isolated_home):
    _isolated_home.mkdir(parents=True, exist_ok=True)
    (_isolated_home / "config.json").write_text("[not an object")
    assert config.load({"CAUCE_HOME": str(_isolated_home)}) == config.DEFAULTS
    (_isolated_home / "config.json").write_text("[1]")
    assert config.load({"CAUCE_HOME": str(_isolated_home)}) == config.DEFAULTS
