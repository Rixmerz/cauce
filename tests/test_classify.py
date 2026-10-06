from __future__ import annotations

import json
import subprocess

import pytest

from cauce.classify import SCHEMA, by_model, by_rules, classify


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Traceback (most recent call last):\n  File x.py, line 3", "debug-repro"),
        ("el build falla con exit code 2 en CI", "debug-repro"),
        ("el test es flaky, a veces pasa", "debug-unclear"),
        ("haz un review del PR de login", "review-critical"),
        ("revisa el codigo del parser", "review-routine"),
        ("propón la arquitectura para el sistema de pagos", "plan"),
        ("¿dónde está definido el router?", "explore"),
        ("haz commit y push", "docs"),
        ("escribe los tests del parser", "test"),
        ("¿qué hace este módulo?", "chat"),
        ("arregla esto #ui", "ui"),
        ("mejora el modelo #fable", "frontier"),
    ],
)
def test_rules_that_fire_are_right(text, kind):
    assert by_rules(text).kind == kind


def test_rules_stay_silent_when_unsure():
    assert by_rules("el header ya no se ve como antes, hay que dejarlo como en el diseño") is None
    assert classify("algo ambiguo para hacer", use_model=False).kind == "implement"
    assert classify("algo ambiguo para hacer", use_model=False).source == "default"


def _runner(stdout: str = "", returncode: int = 0, raises: Exception | None = None):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, "boom")

    run.calls = calls
    return run


def _reply(**out) -> str:
    return json.dumps({"is_error": False, "total_cost_usd": 0.001, "structured_output": out})


def test_model_classification_is_used_when_confident():
    run = _runner(_reply(kind="ui", complexity="low", confidence=0.9, reason="layout"))
    got = by_model("the header looks wrong", runner=run)
    assert (got.kind, got.complexity, got.source, got.cost_usd) == ("ui", "low", "model", 0.001)
    argv, kwargs = run.calls[0]
    assert argv[argv.index("--model") + 1] == "haiku"
    from cauce import models

    models.pin("haiku", "claude-haiku-4-5", None)
    by_model("the header looks wrong", runner=run)
    assert run.calls[-1][0][run.calls[-1][0].index("--model") + 1] == "claude-haiku-4-5"
    models.pin("haiku", None, None)
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert kwargs["input"] == "the header looks wrong"
    assert kwargs["env"]["CAUCE_HOOKS_OFF"] == "1"


def test_frontier_is_not_a_choice_for_the_model():
    assert "frontier" not in SCHEMA["properties"]["kind"]["enum"]
    run = _runner(_reply(kind="frontier", complexity="high", confidence=0.99, reason="x"))
    assert by_model("x", runner=run).kind == "implement"


@pytest.mark.parametrize(
    "runner",
    [
        _runner(_reply(kind="ui", complexity="low", confidence=0.3, reason="?")),
        _runner("not json"),
        _runner(json.dumps([1])),
        _runner(json.dumps({"is_error": True, "result": "x"})),
        _runner(raises=OSError("no claude")),
        _runner(raises=subprocess.TimeoutExpired("claude", 1)),
    ],
)
def test_every_model_failure_is_the_default_kind(runner):
    got = by_model("anything", runner=runner)
    assert got.kind == "implement" and got.source == "default" and got.reason


def test_classify_prefers_rules_and_only_then_asks_the_model():
    run = _runner(_reply(kind="docs", complexity="low", confidence=0.9, reason="r"))
    assert classify("haz commit y push", runner=run).source == "rule"
    assert run.calls == []
    assert classify("algo ambiguo para hacer", runner=run).kind == "docs"


def test_the_neutral_directory_is_created(tmp_path):
    run = _runner(_reply(kind="docs", complexity="low", confidence=0.9, reason="r"))
    target = tmp_path / "not" / "yet"
    assert by_model("x", runner=run, cwd=target).kind == "docs"
    assert target.is_dir() and run.calls[0][1]["cwd"] == str(target)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # what the audited session typed: a change, never a search
        ("Crear 5 módulos de training y 10 rutas lazy en app.routes.ts", "implement"),
        ("Verifica la estructura contra el spec y registra /api/training en index.js", "implement"),
        ("revisa el codigo del parser y arregla lo que encuentres", "implement"),
        ("Find where the router is defined, then add a lazy route", "implement"),
        # a verb of change inside a search is still a search
        ("busca donde se crea el usuario", "explore"),
        ("¿dónde se registra la ruta? y agrega un log", "implement"),
        ("¿dónde se define el usuario que se crea?", "explore"),
        ("where is the router defined #explore, then add a route", "explore"),  # a tag is a person's word
    ],
)
def test_a_request_for_a_change_is_never_read_only(text, kind):
    assert classify(text, use_model=False).kind == kind


def test_the_model_reading_a_change_as_a_search_is_overruled(monkeypatch):
    def fake(argv, **kw):
        out = {"kind": "explore", "complexity": "medium", "confidence": 0.9, "reason": "mentions the spec"}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"structured_output": out, "total_cost_usd": 0.01}), "")

    found = classify("Crear la estructura del frontend según el spec", runner=fake)
    assert found.kind == "implement" and found.source == "model" and found.cost_usd == 0.01
    assert found.reason == "read as explore, but it asks for a change (crear)"
