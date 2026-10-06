from __future__ import annotations

import os

import pytest

from cauce import models
from cauce.store import Store


@pytest.mark.parametrize(
    ("model_id", "family", "version"),
    [
        ("claude-sonnet-5-5", "sonnet", (5, 5)),
        ("claude-sonnet-5", "sonnet", (5,)),
        ("claude-sonnet-4-5-20250929", "sonnet", (4, 5)),
        ("claude-3-5-sonnet-20241022", "sonnet", (3, 5)),
        ("us.anthropic.claude-opus-5-5-v1:0", "opus", (5, 5)),
        ("claude-haiku-4-5-20251001", "haiku", (4, 5)),
        ("claude-fable-5-1", "fable", (5, 1)),
        ("sonnet", "sonnet", None),
        ("<synthetic>", None, None),
        (None, None, None),
    ],
)
def test_a_model_id_says_its_family_and_version(model_id, family, version):
    assert models.family(model_id) == family and models.version(model_id) == version


def test_the_model_that_served_is_the_one_that_did_most_of_the_work():
    usage = {"claude-haiku-4-5": {"outputTokens": 40}, "claude-sonnet-5-5": {"outputTokens": 900},
             "odd": "not a dict"}
    assert models.served({"modelUsage": usage}) == "claude-sonnet-5-5"
    assert models.served({"modelUsage": {}}) == "" and models.served(None) == ""
    assert models.served({"modelUsage": {"m": {"outputTokens": "x"}}}) == "m"


def test_an_alias_follows_claude_code_until_a_person_pins_it(_isolated_home):
    env = dict(os.environ)
    assert models.resolve("sonnet", env) == "sonnet" and models.pinned("sonnet", env) is None
    models.pin("sonnet", "claude-sonnet-5-5", env)
    assert models.resolve("sonnet", env) == "claude-sonnet-5-5"
    assert (_isolated_home / "config.json").is_file()  # the test's own home, never the real one
    assert models.resolve("sonnet", {**env, "CAUCE_MODEL_SONNET": "claude-sonnet-6"}) == "claude-sonnet-6"
    assert models.resolve("sonnet", {**env, "CAUCE_MODEL_SONNET": "bad id; rm -rf"}) == "claude-sonnet-5-5"
    models.pin("sonnet", None, env)
    assert models.resolve("sonnet", env) == "sonnet"
    with pytest.raises(ValueError, match="unknown model"):
        models.pin("gpt", "x", env)
    with pytest.raises(ValueError, match="does not look like a model id"):
        models.pin("opus", "two words", env)


def test_workers_behind_the_person_s_sessions_are_named_with_the_pin(store: Store):
    for i, model in enumerate(("claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5")):
        store.add_usage(message_id=f"m{i}", session_id="s", model=model, output_tokens=1, ts="2099-01-01")
    t = store.create_task("x", status="done", source="cauce")
    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, served_model="claude-sonnet-5")
    store.add_attempt(t["id"], cell="opus/high", max_turns=30, passed=1, served_model="claude-opus-5-5")
    assert store.worker_models() == {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5"}
    assert models.lagging(store, dict(os.environ)) == [{"alias": "sonnet", "workers": "claude-sonnet-5",
                                         "sessions": "claude-sonnet-5-5",
                                         "command": "cauce config model sonnet claude-sonnet-5-5"}]
    assert models.lagging(store, {**os.environ, "CAUCE_MODEL_SONNET": "claude-sonnet-5-5"}) == []
