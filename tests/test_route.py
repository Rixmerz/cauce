"""Which way a request is done: the session itself, one worker, or parts side by side."""
from __future__ import annotations

import json

from cauce import hooks, route
from cauce.store import Store

SMALL = "Fix the typo in the README heading of the install section"
MEDIUM = ("Add a --json flag to the export command in src/cli/export.py that prints the rows as JSON, "
          "with a test in tests/test_export.py")
BIG = ("Build the billing module: a schema migration for invoices, the payment webhook with its signature check, "
       "the admin screens to list and refund them, and tests for each, in src/billing/models.py, "
       "src/billing/webhook.py, src/admin/invoices.tsx and src/billing/refund.py")
FOOTPRINT = (2_300_000, 26)


def test_a_short_turn_reading_and_the_conversation_stay_with_the_session():
    assert route.choose("ok go on").mode == route.MAIN
    read = route.choose("Explain how the dispatcher decides which queued task starts next in this repository")
    assert read.mode == route.MAIN and "reading" in read.reasons[0]
    said = route.choose("Apply this to the export command as we discussed above, with its test",
                        context_tokens=600_000, footprint=FOOTPRINT)
    assert said.mode == route.MAIN and "this conversation" in said.reasons[0] and said.advice() == ""


def test_large_or_spread_work_runs_in_parts():
    found = route.choose(BIG, context_tokens=50_000, footprint=FOOTPRINT)
    assert found.mode == route.PARALLEL and "cauce workflow run feature" in found.advice()
    pieces = ("Do these:\n- update src/api/users.py for the new field\n- update web/views/users.tsx to show it\n"
              "- update docs/guide/users.md to describe it")
    assert route.choose(pieces).mode == route.PARALLEL
    assert "3 listed pieces in separate folders" in route.choose(pieces).reasons


def test_the_cheaper_reading_decides_once_it_is_measured():
    # A session with a large context re-reads all of it each turn: a worker reads less.
    heavy = route.choose(MEDIUM, context_tokens=600_000, footprint=FOOTPRINT)
    assert heavy.mode == route.WORKER and "about 3.0M tokens (5 turns over this session's 600k context)" \
        in heavy.reasons[0] and "a worker about 442k at its measured 88k a turn" in heavy.reasons[0]
    assert "Queue it" in heavy.advice() and "do it yourself instead" in heavy.advice()
    light = route.choose(MEDIUM, context_tokens=40_000, footprint=FOOTPRINT)
    assert light.mode == route.MAIN and "200k" in light.reasons[0] and light.advice() == ""
    assert route.choose(SMALL, context_tokens=600_000, footprint=FOOTPRINT).mode == route.WORKER


def test_without_a_measure_size_and_a_full_context_decide():
    assert route.choose(SMALL).mode == route.MAIN
    listed = ("Implement the export:\n- S1 json\n- S2 csv\n- S3 a test in tests/test_export.py\n"
              "- S4 docs in docs/export.md")
    assert route.choose(listed).mode == route.WORKER and "3 points" in route.choose(listed).reasons[0]
    full = route.choose(SMALL, context_share=0.7)
    assert full.mode == route.WORKER and "70% full" in full.reasons[0]
    assert route.choose(MEDIUM).data()["kind"] == "implement" and route._tokens(999) == "1k"
    assert route.choose("Why does the export drop the last row when the file is empty?").mode == route.MAIN
    assert route.choose("Fix the export so this repository's tests pass again", context_tokens=600_000,
                        footprint=FOOTPRINT).mode == route.WORKER  # "this repository" is no pointer back


def test_a_worker_s_footprint_is_measured_from_its_attempts(store: Store):
    assert store.worker_footprint("r") is None
    for i in range(6):
        task = store.create_task(f"t{i}", status="done", source="cauce", repo="r")
        store.add_event(task["id"], "attempt_finished", input_tokens=1_000_000 * (i + 1), turns=10 + i)
    assert store.worker_footprint("r") == (4_000_000, 13)
    assert store.worker_footprint("elsewhere") == (4_000_000, 13)  # anywhere, when the repository has too few
    assert store.worker_footprint(None) == (4_000_000, 13)


def test_the_prompt_hook_advises_and_keeps_each_decision(store: Store, git_repo, tmp_path, monkeypatch):
    from cauce import project

    project.enroll(git_repo)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-20T10:00:00Z", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 599_990,
                                              "cache_creation_input_tokens": 0}}}) + "\n")
    monkeypatch.setattr(store, "worker_footprint", lambda key: FOOTPRINT)
    event = {"session_id": "s", "prompt": MEDIUM, "prompt_id": "p", "cwd": str(git_repo),
             "transcript_path": str(transcript)}
    assert hooks.user_prompt_submit(event, store) is None  # the setting is off in tests
    monkeypatch.setenv("CAUCE_ROUTE", "on")
    out = hooks.user_prompt_submit({**event, "prompt_id": "p2"}, store)
    said = out["hookSpecificOutput"]["additionalContext"]
    assert said.startswith("cauce route: a worker (done here it reads about")
    routed = store.session_events("s", ("routed",))[-1]["data"]
    assert routed["mode"] == "worker" and routed["context_tokens"] == 600_000 and routed["worker_tokens"] == 2_300_000
    # The session does it: kept, and nothing is said.
    assert hooks.user_prompt_submit({**event, "prompt": SMALL, "prompt_id": "p3", "transcript_path": None},
                                    store) is None
    assert store.session_events("s", ("routed",))[-1]["data"]["mode"] == "main"
    # Outside a project, nothing; a failure costs the turn nothing.
    assert hooks._route(store, 1, MEDIUM, str(tmp_path), None) is None

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(route, "choose", broken)
    assert hooks._route(store, 1, MEDIUM, str(git_repo), None) is None


def test_a_question_about_the_work_stays_with_the_session_and_an_order_with_one_does_not():
    asked = ("También se podría reutilizar siempre la misma sesión por cada rol para tener nociones generales "
             "quizás? Al menos una sesión por rol por proyecto")
    found = route.choose(asked, context_tokens=600_000, footprint=FOOTPRINT)
    assert found.mode == route.MAIN and "a question about the work" in found.reasons[0]
    assert route.asks("Should we cache the parsed config between runs, or is it cheap enough?")
    assert route.asks("Se ve bien. ¿Vale la pena limitar los turnos por intento en cada tipo de tarea?")
    order = "Fix the export so the tests pass again. Could it also log the row count while it is at it?"
    assert not route.asks(order)
    assert route.choose(order, context_tokens=600_000, footprint=FOOTPRINT).mode == route.WORKER
    assert not route.asks(MEDIUM)
