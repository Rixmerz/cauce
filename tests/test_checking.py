"""What was asked, findings as records, risk, and the independent check."""
from __future__ import annotations

import json
import subprocess

import pytest

from cauce import criteria, findings, launch, risk
from cauce.escalate import Attempt, Failure, Move, decide
from cauce.launch import LaunchSpec, WorkerResult
from cauce.matrix import LADDERS, Cell
from cauce.orchestrate import Options, run, verifier_brief, verify_cell
from cauce.store import home

from .test_orchestrate import Script, kind


# --- criteria -------------------------------------------------------------------------


def test_listed_items_become_criteria_in_the_person_s_words():
    text = "Fix the cart:\n- totals round once\n* coupons expire at midnight UTC\n2) keep the API as it is\n"
    assert criteria.items(text) == ["totals round once", "coupons expire at midnight UTC", "keep the API as it is"]
    assert criteria.ids(text) == ("S1", "S2", "S3")
    assert "S2: coupons expire at midnight UTC" in criteria.brief(text)
    assert criteria.items("one sentence. Another sentence.") == [] and criteria.brief("- only one") == ""
    assert criteria.read({"criteria": [{"id": "s1", "met": True}, {"id": "S2"}, "x"]}) == (
        {"id": "S1", "met": True, "evidence": ""},)
    assert criteria.read({"criteria": "no"}) == ()
    assert criteria.unmet(("S1", "S2", "S3"), [{"id": "S1", "met": True}, {"id": "S2", "met": False}]) == (
        ["S3"], ["S2"])


def proc(block: dict) -> subprocess.CompletedProcess:
    text = "done\n```cauce-result\n" + json.dumps(block) + "\n```"
    return subprocess.CompletedProcess([], 0, json.dumps({"result": text, "total_cost_usd": 0.1}), "")


def test_a_pass_must_say_each_criterion_is_met(tmp_path):
    spec = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, criteria=("S1", "S2"))
    ok = {"verdict": "pass", "summary": "s", "evidence": "1 passed"}
    both = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}, {"id": "S2", "met": True}]}), spec)
    assert both.passed and len(both.criteria) == 2
    left_out = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}]}), spec)
    assert not left_out.passed and left_out.failure is Failure.INCONCLUSIVE and "S2" in left_out.summary
    not_met = launch.parse(proc({**ok, "criteria": [{"id": "S1", "met": True}, {"id": "S2", "met": False}]}), spec)
    assert not not_met.passed and not_met.failure is Failure.CODE_BUG and "S2 is not met" in not_met.summary
    plain = launch.parse(proc(ok), LaunchSpec("p", Cell("sonnet", "medium"), tmp_path))
    assert plain.passed


def test_the_brief_carries_the_criteria(git_repo, store):
    script = Script(WorkerResult(True, None, "ok", "1 passed", criteria=({"id": "S1", "met": True},
                                                                          {"id": "S2", "met": True})))
    run("Do it:\n- one\n- two", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "S1: one" in script.specs[0].prompt and script.specs[0].criteria == ("S1", "S2")


# --- findings -------------------------------------------------------------------------


def test_findings_are_records_and_only_the_change_s_own_shown_ones_block():
    read = findings.read({"findings": [
        {"where": "src/a.py:12", "what": "Off by one in the loop", "evidence_class": "deterministic",
         "cause": "introduced", "proof": "pytest -k loop"},
        {"where": "src/b.py", "what": "old bug", "evidence_class": "inferential", "cause": "pre-existing"},
        {"where": "src/c.py", "what": "maybe slow", "evidence_class": "insufficient", "cause": "introduced"},
        {"what": "no place, odd values", "evidence_class": "weird", "cause": "??"},
        {"where": "x"}, "junk"]})
    assert len(read) == 4 and read[3]["evidence_class"] == "inferential" and read[3]["cause"] == "unknown"
    assert [f["where"] for f in findings.blocking(read)] == ["src/a.py:12", ""]
    moved = {"where": "src/a.py:40", "what": "reworded entirely", "evidence_class": "deterministic",
             "cause": "introduced"}
    assert findings.same(read[0], moved) and not findings.same(read[1], moved)
    assert findings.places(read) == {"src/a.py", "src/b.py", "src/c.py"}
    assert "- [introduced, deterministic] src/a.py:12: Off by one in the loop — pytest -k loop" in findings.text(read)
    assert findings.read({"findings": "no"}) == ()


def test_two_reviews_that_place_the_same_findings_converge():
    a = Attempt(Cell("sonnet", "medium"), 30, False, Failure.CODE_BUG, "words one",
                findings=({"where": "a.py:1", "what": "x"},))
    b = Attempt(Cell("sonnet", "high"), 30, False, Failure.CODE_BUG, "entirely other words",
                findings=({"where": "a.py:9", "what": "y"},))
    assert decide(LADDERS["review-routine"], [a, b], reading=True).move is Move.CONVERGED
    c = Attempt(Cell("sonnet", "high"), 30, False, Failure.CODE_BUG, "words one",
                findings=({"where": "b.py", "what": "y"},))
    assert decide(LADDERS["review-routine"], [a, c], reading=True).move is not Move.CONVERGED


# --- risk -----------------------------------------------------------------------------


def test_risk_starts_from_the_request_and_only_rises_with_the_change():
    assert risk.of_request("explain the parser", "explore").tier == "passive"
    assert risk.of_request("rename a helper", "refactor").tier == "medium"
    r = risk.of_request("fix the login token refresh", "implement")
    assert r.tier == "high" and "security" in r.reasons[0]
    assert risk.of_request("audit it", "review-critical").tier == "high"
    r = risk.of_change(risk.of_request("rename", "refactor"), ["src/a.py", "tests/test_a.py"])
    assert r.tier == "medium"
    r = risk.of_change(risk.of_request("rename", "refactor"), ["src/a.py"])
    assert r.tier == "high" and "no test beside it" in r.reasons[-1]
    assert risk.of_change(risk.Risk("medium"), ["db/migrations/0002.sql"]).tier == "high"
    assert risk.of_change(risk.Risk("medium"), None).tier == "high"
    docs = risk.of_change(risk.Risk("medium"), ["README.md"])
    assert docs.tier == "medium" and "documentation only" in docs.reasons[-1]
    high = risk.Risk("high")
    high.raise_to("passive", "x")
    assert high.tier == "high"
    assert risk.needs_verifier(risk.Risk("high"), cheap=False)
    assert risk.needs_verifier(risk.Risk("medium"), cheap=True) and not risk.needs_verifier(risk.Risk("medium"),
                                                                                             cheap=False)


# --- the independent check --------------------------------------------------------------


def passing(summary="done"):
    return WorkerResult(True, None, summary, "1 passed", cost_usd=0.1)


def check(verdict="pass", found=(), summary="checked"):
    return WorkerResult(verdict == "pass", None if verdict == "pass" else Failure.CODE_BUG, summary, "probe: ok",
                        cost_usd=0.05, verdict=verdict, findings=tuple(found))


BUG = {"where": "app.py:3", "what": "drops the last item", "evidence_class": "deterministic", "cause": "introduced",
       "proof": "python probe.py"}
OTHER = {"where": "lib.py:9", "what": "a new crash", "evidence_class": "deterministic", "cause": "introduced",
         "proof": ""}
OLD = {"where": "old.py", "what": "was always so", "evidence_class": "deterministic", "cause": "pre-existing",
       "proof": ""}


@pytest.fixture
def verifier_on(monkeypatch):
    monkeypatch.setenv("CAUCE_VERIFIER", "on")


def test_a_risky_pass_is_checked_and_its_findings_earn_one_correction(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG, OLD]), passing("fixed"), check("pass", [OLD]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and report.cells == ["sonnet/medium", "sonnet/high"]
    worker, verifier, again, recheck = script.specs
    assert verifier.cell == Cell("sonnet", "high") and set(launch.WRITE_TOOLS) <= set(verifier.disallowed_tools)
    assert "fix the cart total" in verifier.prompt and "done" not in verifier.prompt.split("verbatim:")[1][:40]
    assert verifier.append_system_prompt.startswith("You work as the `verifier` role")
    assert "drops the last item" in again.prompt  # the findings are the next attempt's evidence
    assert "drops the last item" in recheck.prompt and "whether it is fixed" in recheck.prompt
    events = store.events_of(report.task_id, "verified")
    assert [e["data"]["blocking"] for e in events] == [1, 0] and events[0]["data"]["risk"]["tier"] == "high"
    attempts = store.attempts(report.task_id)
    assert attempts[0]["cost_usd"] == pytest.approx(0.15)
    assert "risk high" in report.text()


def test_a_new_finding_after_a_correction_stops_for_a_person(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("fail", [OTHER]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and report.stop.cause == "review" and "new finding" in report.stop.reason
    assert report.branch and "a new crash" in report.stop.account


def test_the_same_problem_twice_earns_a_second_correction_then_a_person(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), passing(), check("fail", [BUG]), passing(),
                    check("fail", [{**BUG, "where": "app.py:7"}]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and "survived 2 corrections" in report.stop.reason
    assert len(report.cells) == 3


def test_an_undecided_check_or_a_fail_without_findings(git_repo, store, verifier_on):
    script = Script(passing(), check("inconclusive", summary="no browser"), write="app.py")
    report = run("fix it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    assert "independent check inconclusive: no browser" in store.attempts(report.task_id)[0]["evidence"]
    script = Script(passing(), check("fail", summary="the totals are wrong"), passing(), check("pass"),
                    write="app.py")
    report = run("fix it", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and "the totals are wrong" in script.specs[2].prompt


def test_no_check_for_a_change_without_risk_or_a_reading_task(git_repo, store, verifier_on):
    script = Script(passing(), write="notes.md")
    run("rename the docs heading", git_repo, store, registry={}, launcher=script, classifier=kind("refactor"))
    assert len(script.specs) == 1  # documentation only: medium, from a strong cell
    script = Script(passing())
    run("review the cart", git_repo, store, registry={}, launcher=script, classifier=kind("review-routine"))
    assert len(script.specs) == 1
    script = Script(passing(), check("pass"), write="notes.md")
    run("rename the docs heading", git_repo, store, Options(start=Cell("haiku", "medium")), registry={},
        launcher=script, classifier=kind("refactor"))
    assert len(script.specs) == 2 and script.specs[1].cell == Cell("sonnet", "high")  # a cheap cell's pass


def test_the_verifier_cell_and_brief():
    assert verify_cell(Cell("haiku", "low")) == Cell("sonnet", "high")
    assert verify_cell(Cell("opus", "medium")) == Cell("opus", "high")
    assert verify_cell(Cell("opus", "max")) == Cell("opus", "max")
    text = verifier_brief("Do:\n- a\n- b", "implement", home(), None, [f"f{i}" for i in range(25)])
    assert "S2: b" in text and "uncommitted" in text and "and 5 more" in text


def test_a_person_s_verifier_role_takes_the_bundled_one_s_place():
    from cauce import roles

    from .test_roles import write_role

    assert roles.internal("verifier").scope == "bundled"
    assert "verifier" not in roles.load().roles  # in no team
    write_role(home() / "roles", "verifier", "Mine.", description="mine", kinds=["review-routine"])
    assert roles.internal("verifier").prompt == "Mine."
