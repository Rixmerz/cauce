from __future__ import annotations

import os

import pytest

from cauce import grants


def test_workers_run_in_auto_mode_and_haiku_bypasses(_isolated_home):
    env = dict(os.environ)
    assert grants.mode_for("sonnet", env) == "auto" and grants.mode_for("opus", env) == "auto"
    assert grants.mode_for("haiku", env) == "bypassPermissions"
    grants.set_mode("haiku", "acceptEdits", env)
    grants.set_mode("default", "dontAsk", env)
    assert grants.mode_for("haiku", env) == "acceptEdits" and grants.mode_for("opus", env) == "dontAsk"
    assert grants.mode_for("opus", {**env, "CAUCE_MODE_OPUS": "auto"}) == "auto"
    assert grants.mode_for("opus", {**env, "CAUCE_MODE": "bypassPermissions"}) == "bypassPermissions"
    assert grants.mode_for("opus", {**env, "CAUCE_MODE": "plan"}) == "dontAsk"  # never a mode that waits
    grants.set_mode("haiku", None, env)
    grants.set_mode("default", None, env)
    assert grants.mode_for("haiku", env) == "bypassPermissions" and grants.mode_for("opus", env) == "auto"
    with pytest.raises(ValueError, match="unknown mode"):
        grants.set_mode("opus", "plan", env)


def test_rules_are_kept_per_repository_in_the_person_s_home(_isolated_home):
    env = dict(os.environ)
    assert grants.granted("github.com/o/r", env) == () and grants.granted(None, env) == ()
    rules = grants.expand(["Bash(make:*)", " "], ["node"])
    assert rules == ("Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(make:*)")
    assert grants.grant("github.com/o/r", rules, env) == rules
    assert grants.grant("github.com/o/r", ["Bash(make:*)"], env) == rules  # no duplicates
    assert grants.granted("github.com/o/other", env) == ()
    assert grants.revoke("github.com/o/r", ["Bash(make:*)"], env) == rules[:3]
    assert grants.revoke("github.com/o/r", [], env) == ()
    assert "grants" in (_isolated_home / "config.json").read_text()
    with pytest.raises(ValueError, match="unknown preset"):
        grants.expand([], ["everything"])
