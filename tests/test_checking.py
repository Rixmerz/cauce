"""What was asked, findings as records, risk, and the independent check."""
from __future__ import annotations

import json
import subprocess

import pytest

from cauce import criteria, findings, flow, launch, risk
from cauce.escalate import Attempt, Failure, Move, decide
from cauce.launch import LaunchSpec, WorkerResult
from cauce.matrix import LADDERS, Cell
from cauce.orchestrate import Options, run, verifier_brief, verify_cell
from cauce.store import home

from .test_orchestrate import Script, kind, ok
from .test_ui import ui  # noqa: F401  (the fixture)

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
    _, verifier, again, recheck = script.specs
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


def test_progress_earns_the_next_round_and_what_is_left_at_the_cap_goes_to_a_person(git_repo, store, verifier_on):
    """The palette that never passed: each round fixed what the one before found, and the
    check showed something new. Fixing every earlier problem is progress, and earns a round
    up to the cap; what is still shown there stops the task for a person."""
    third = {**OTHER, "where": "theme.css:4", "what": "a third contrast failure"}
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("fail", [OTHER]), passing("fixed too"),
                    check("fail", [third]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert len(report.cells) == 3 and "a new crash" in script.specs[4].prompt
    assert report.status == "blocked" and report.stop.cause == "review"
    assert "still shows problems the earlier checks had not" in report.stop.reason
    assert report.branch and "a third contrast failure" in report.stop.account


def test_after_progress_a_finding_only_read_from_the_code_goes_with_the_pass(git_repo, store, verifier_on):
    read = {**OTHER, "evidence_class": "inferential", "what": "the hover colour may be too light"}
    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("fail", [read]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "done" and len(script.specs) == 4
    events = store.events_of(report.task_id, "verified")
    assert [(e["data"]["blocking"], e["data"]["carried"]) for e in events] == [(1, 0), (0, 1)]
    assert "the hover colour may be too light" in store.attempts(report.task_id)[-1]["evidence"]
    finished = store.last_event(report.task_id, "finished")["data"]
    assert any(f["what"] == read["what"] for f in finished["findings"])


def test_a_problem_that_survives_beside_new_ones_stops_at_the_cap(git_repo, store, verifier_on):
    script = Script(passing(), check("fail", [BUG]), passing(), check("fail", [BUG, OTHER]), passing(),
                    check("fail", [BUG, OTHER]), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert report.status == "blocked" and "still there, beside new ones" in report.stop.reason


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
    assert verify_cell(Cell("opus", "max")) == Cell("opus", "high")  # independence is the session, not effort
    text = verifier_brief("Do:\n- a\n- b", "implement", home(), None, [f"f{i}" for i in range(25)])
    assert "S2: b" in text and "uncommitted" in text and "and 5 more" in text


def test_a_person_s_verifier_role_takes_the_bundled_one_s_place():
    from cauce import roles

    from .test_roles import write_role

    assert roles.internal("verifier").scope == "bundled"
    assert "verifier" not in roles.load().roles  # in no team
    write_role(home() / "roles", "verifier", "Mine.", description="mine", kinds=["review-routine"])
    assert roles.internal("verifier").prompt == "Mine."


# --- surfaces -------------------------------------------------------------------------


def test_surfaces_hold_what_they_name_and_nothing_else():
    from cauce import surfaces

    s = surfaces.check(["src/api/", "docs/*.md", "tests/**", "lib", "*.cfg", "./", " "])
    assert s == ("src/api/**", "docs/*.md", "tests/**", "lib", "*.cfg", "**")
    assert surfaces.allows(("src/api/**",), "src/api/x/y.py") and not surfaces.allows(("src/api/**",), "src/apix.py")
    assert surfaces.allows(("docs/*.md",), "docs/a.md") and not surfaces.allows(("docs/*.md",), "docs/x/a.md")
    assert surfaces.allows(("lib",), "lib/a/b.c") and not surfaces.allows(("lib",), "libx")
    assert surfaces.allows(("a[bc].py",), "ab.py") and surfaces.allows(("a[b",), "a[b")
    assert surfaces.allows(("src/?.py",), "src/a.py") and not surfaces.allows(("src/?.py",), "src/ab.py")
    assert surfaces.outside((), ["anything"]) == [] and surfaces.outside(("src/**",), ["src/a", "b"]) == ["b"]
    with pytest.raises(surfaces.SurfaceError, match="inside the project"):
        surfaces.check(["../up"])
    with pytest.raises(surfaces.SurfaceError, match="at most"):
        surfaces.check([f"d{i}" for i in range(30)])
    assert surfaces.disjoint(["src/api/**"], ["src/web/**"]) and surfaces.disjoint(["src/a.py"], ["src/b.py"])
    assert not surfaces.disjoint(["src/**"], ["src/web/a.ts"]) and not surfaces.disjoint(["*.md"], ["src/x"])
    assert not surfaces.disjoint([], ["src/x"]) and not surfaces.disjoint(["src/x"], ["src/x"])
    assert surfaces.brief(()) == "" and "src/api/**" in surfaces.brief(("src/api/**",))


def test_a_change_outside_the_surfaces_is_undone_and_fails_the_attempt(git_repo, store):
    seen = []

    def launcher(spec):
        seen.append(spec)
        (spec.target_dir / "app.py").write_text("changed\n")  # tracked at the base: restored
        if len(seen) == 1:
            (spec.target_dir / "stray.txt").write_text("x\n")  # new: removed
            return WorkerResult(True, None, "done", "1 passed", changed_paths=("app.py", "stray.txt"))
        return WorkerResult(True, None, "done", "1 passed", changed_paths=("src/ok.py",))

    report = run("fix it", git_repo, store, Options(surfaces=("src/**",)), registry={}, launcher=launcher,
                 classifier=kind("implement"))
    assert "Change only files inside these surfaces: src/**" in seen[0].prompt
    first = store.attempts(report.task_id)[0]
    assert not first["passed"] and first["failure"] == "approach" and "outside its surfaces" in first["summary"]
    event = store.last_event(report.task_id, "outside_surfaces")["data"]
    assert event["paths"] == ["app.py", "stray.txt"] and event["undone"]
    assert Options(surfaces=("a",)).stored()["surfaces"] == ["a"]


def test_in_the_checkout_itself_nothing_is_undone(git_repo, store):
    def launcher(spec):
        return WorkerResult(True, None, "done", "1 passed", changed_paths=("app.py",))

    report = run("fix it", git_repo, store, Options(surfaces=("src/**",), isolate=False, max_attempts=1),
                 registry={}, launcher=launcher, classifier=kind("implement"))
    assert "left in your checkout" in store.attempts(report.task_id)[0]["summary"]


def test_tasks_whose_surfaces_cannot_touch_run_side_by_side_without_a_model(store):
    from cauce import flow

    def queued(text, surf):
        return store.enqueue(text, repo="k", cwd="/x", options={"surfaces": surf} if surf else {})

    a, b = queued("a", ["src/api/**"]), queued("b", ["src/web/**"])
    assert flow.by_surfaces(b, [a]).parallel
    c = queued("c", ["src/**"])
    decision = flow.by_surfaces(c, [a])
    assert not decision.parallel and f"#{a['id']}" in decision.reason
    assert flow.by_surfaces(queued("d", None), [a]) is None and flow.by_surfaces(b, [queued("e", None)]) is None
    assert flow.by_surfaces(b, []) is None
    assert flow._surfaces_of({"options": "{"}) == ()


def test_a_workflow_step_carries_its_surfaces(git_repo, store, monkeypatch):
    from cauce import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    with pytest.raises(workflows.WorkflowError, match="surfaces are a list"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "surfaces": "src"}]})
    with pytest.raises(workflows.WorkflowError, match="inside the project"):
        workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p", "surfaces": ["../x"]}]})
    assert "surfaces" not in workflows.normalize({"name": "x", "steps": [{"id": "a", "prompt": "p",
                                                                         "surfaces": []}]})["steps"][0]
    workflows.save({"name": "bounded", "steps": [{"id": "a", "prompt": "Do it", "surfaces": ["src/"]}]}, git_repo,
                   "user")
    started = workflows.start(store, "bounded", git_repo)
    assert json.loads(store.get_task(started["queued"][0])["options"])["surfaces"] == ["src/**"]


def test_the_cli_takes_surfaces(capsys, git_repo):
    from cauce import cli

    assert cli.main(["queue", "add", "x", "--repo", str(git_repo), "--surface", "src/", "--json"]) == 0
    from cauce.store import Store

    queued = json.loads(capsys.readouterr().out)["id"]
    store = Store.open()
    assert json.loads(store.get_task(queued)["options"])["surfaces"] == ["src/**"]
    store.close()
    with pytest.raises(SystemExit, match="inside the project"):
        cli.main(["run", "x", "--surface", "/abs", "--repo", str(git_repo), "--dry-run"])


# --- the blind dual review --------------------------------------------------------------


def test_the_dual_review_starts_two_blind_reviews_on_different_models(git_repo, store, monkeypatch):
    from cauce import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    defn = workflows.load("dual-review", git_repo)
    steps = {s["id"]: s for s in defn["steps"]}
    assert steps["review-a"]["start"] != steps["review-b"]["start"]
    assert steps["refute"]["needs"] == ["review-a", "review-b"] and steps["recheck"]["needs"] == ["fix"]
    started = workflows.start(store, "dual-review", git_repo, {"target": "the cart module"})
    first = [store.get_task(i) for i in started["queued"]]
    assert len(first) == 2 and all("one of two reviewers working apart" in t["body"] for t in first)
    # neither review is shown the other's: they start together, before anything is found
    assert all("What the steps before it found" not in t["body"] for t in first)


# --- what a running attempt is doing, and the change it kept -----------------------------


def log(entries):
    import os
    from datetime import UTC, datetime

    from cauce import signature

    folder = signature.log_dir(dict(os.environ))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{datetime.now(UTC):%Y-%m-%d}.ndjson").open("a") as fh:
        for task_id, attempt, sig in entries:
            fh.write(json.dumps({"task_id": task_id, "attempt": attempt, "sig": sig}) + "\n")
        fh.write("not json\n")


def test_a_running_attempt_s_phase_is_read_from_its_last_calls():
    from cauce import phase

    assert phase.of(1, 1) is None
    log([(1, 1, "read:py"), (1, 1, "grep")])
    assert phase.of(1, 1) == "exploring"
    log([(1, 1, "edit:py"), (2, 1, "bash:pytest")])
    assert phase.of(1, 1) == "implementing" and phase.of(2, 1) == "checking"
    log([(1, 1, "bash:npm:test")])
    assert phase.of(1, 1) == "checking" and phase.of(1, None) == "checking" and phase.of(1, 2) is None
    assert phase.kind_of("write:ts") == "implementing" and phase.kind_of("bash:ls") == "exploring"


def test_the_board_and_the_drawer_show_the_phase_the_risk_and_the_checks(git_repo, store, verifier_on):
    from cauce.ui import api

    script = Script(passing(), check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    detail = api.task_detail(store, report.task_id)
    assert detail["risk"]["tier"] == "high" and len(detail["checks"]) == 2
    assert detail["checks"][0]["findings"][0]["where"] == "app.py:3" and detail["phase"] is None
    running = store.create_task("busy", status="running", source="cauce", repo="k", cwd=str(git_repo))
    store.add_event(running["id"], "attempt_started", seq=1, cell="sonnet/medium")
    log([(running["id"], 1, "edit:py")])
    board = api.board(store)
    assert next(t for t in board["running"] if t["id"] == running["id"])["phase"] == "implementing"
    assert api.task_detail(store, running["id"])["phase"] == "implementing"
    diff = api.task_diff(store, report.task_id)
    assert diff["branch"] == report.branch and "app.py" in diff["diff"] and not diff["cut"]
    assert api.task_diff(store, 999) is None
    assert "no kept branch" in api.task_diff(store, running["id"])["why"]
    gone = store.create_task("x", status="done", source="cauce", cwd="/nowhere")
    assert "not here" in api.task_diff(store, gone["id"])["why"]


def test_the_diff_route_answers(ui, store, git_repo):  # noqa: F811
    from .test_ui import call

    task = store.create_task("x", status="done", source="cauce", cwd=str(git_repo))
    res, data = call(ui, "GET", f"/api/tasks/{task['id']}/diff")
    assert res.status == 200 and "branch" in json.loads(data)
    res, _ = call(ui, "GET", "/api/tasks/99999/diff")
    assert res.status == 404


# --- the ledger: a refuter, never a vote --------------------------------------------------


def _found(*rows):
    from cauce import findings

    return findings.read({"findings": [dict(zip(("where", "what", "evidence_class", "cause"), r, strict=True))
                                       for r in rows]})


def _decides(summary, **verdicts):
    from cauce import ledger
    from cauce.launch import WorkerResult

    return WorkerResult(True, None, summary, evidence="read it",
                        verdicts=ledger.read({"verdicts": [{"id": k, "verdict": v} for k, v in verdicts.items()]}))


def _reviewed(summary, *rows):
    from cauce.launch import WorkerResult

    return WorkerResult(True, None, summary, evidence="read it", findings=_found(*rows))


def test_the_ledger_adds_reviews_together_and_sends_only_one_sided_readings_to_a_refuter():
    from cauce import ledger

    rows = ledger.collect([
        ("review-a", _found(("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
                            ("cart.py:40", "rounding drops a cent", "inferential", "introduced"),
                            ("old.py:1", "an old leak", "deterministic", "pre-existing"))),
        ("review-b", _found(("cart.py:10", "the discount is never applied", "inferential", "introduced"),
                            ("cart.py:41", "rounding drops a cent", "inferential", "introduced"),
                            ("tax.py:5", "tax on the gross", "inferential", "worsened"))),
    ])
    # one reviewer is enough to put a problem on it; a pre-existing one never is
    assert [(r["id"], r["where"], r["seen_by"]) for r in rows] == [
        ("F1", "cart.py:10", ["review-a", "review-b"]), ("F2", "cart.py:40", ["review-a", "review-b"]),
        ("F3", "tax.py:5", ["review-b"])]
    assert [r["id"] for r in rows if ledger.disputed(r)] == ["F3"]
    assert ledger.digest(rows) == ledger.digest(ledger.collect([("review-a", _found(
        ("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
        ("cart.py:40", "rounding drops a cent", "inferential", "introduced"))), ("review-b", _found(
        ("cart.py:10", "the discount is never applied", "inferential", "introduced"),
        ("cart.py:41", "rounding drops a cent", "inferential", "introduced"),
        ("tax.py:5", "tax on the gross", "inferential", "worsened")))]))
    text = ledger.brief("refute", rows[2:], round_=1, sha="abc")
    assert "never decide a row by who reported it" in text and "F3" in text and "confirmed, refuted, unsure" in text

    refuted = ledger.outcome({"role": "refute", "ids": ["F3"], "agreed": ["F1"]}, [{"id": "F3", "verdict": "x"}])
    assert refuted.confirmed == ["F1"] and refuted.held == ["F3"] and not refuted.stop
    alone = ledger.outcome({"role": "refute", "ids": ["F3"], "agreed": []}, [])
    assert "could not decide F3" in alone.stop
    checked = ledger.outcome({"role": "recheck", "round": 2, "ids": ["F1", "F2"]},
                             [{"id": "F1", "verdict": "fixed"}, {"id": "F2", "verdict": "regression"}])
    assert checked.survivors == ["F2"] and "after 2 fix rounds" in checked.stop
    clean = ledger.outcome({"role": "recheck", "round": 1, "ids": ["F1"], "held": ["F3"]},
                           [{"id": "F1", "verdict": "fixed"}])
    assert not clean.survivors and "could not decide F3" in clean.stop
    assert ledger.read({"verdicts": [{"id": "f1", "verdict": " Fixed "}, "junk", {"id": 3}]}) == (
        {"id": "F1", "verdict": "fixed", "proof": ""},)
    assert ledger.read({}) == ()


def test_a_deciding_pass_answers_every_row(tmp_path):
    from cauce.launch import parse

    spec = LaunchSpec("p", Cell("sonnet", "medium"), tmp_path, verdicts=("F1", "F2"),
                      verdict_words=("fixed", "not_fixed", "regression"))
    block = {"verdict": "pass", "summary": "checked", "evidence": "read it",
             "verdicts": [{"id": "F1", "verdict": "fixed"}, {"id": "F2", "verdict": "maybe"}]}
    result = parse(proc(block), spec)
    assert not result.passed and "without a verdict on F2" in result.summary
    block["verdicts"][1]["verdict"] = "not_fixed"
    assert parse(proc(block), spec).passed


def _ledger_run(git_repo, store, results, monkeypatch):
    from cauce import workflows

    monkeypatch.setattr(workflows, "_kick", lambda cwd, key: None)
    started = workflows.start(store, "dual-review", git_repo, {"target": "the cart"}, session_id="s1")
    seen: list[dict] = []
    from .test_workflows import _runner

    flow.work(store, parallel=False, runner=_runner(results, seen))
    return started, seen, {t["workflow_step"]: t for t in store.run_tasks(started["id"])}


def test_the_dual_review_fixes_only_what_is_confirmed_and_rechecks_each_row(git_repo, store, monkeypatch):
    from cauce import workflows

    results = [
        _reviewed("a", ("cart.py:10", "total ignores the discount", "deterministic", "introduced"),
                  ("cart.py:40", "rounding drops a cent", "inferential", "introduced")),
        _reviewed("b", ("cart.py:10", "the discount is never applied", "inferential", "introduced"),
                  ("tax.py:5", "tax on the gross", "inferential", "worsened")),
        _decides("decided", F2="confirmed", F3="refuted"),
        ok("fixed both"),
        _decides("one left", F1="fixed", F2="not_fixed"),
        ok("fixed the rounding"),
        _decides("all fixed", F2="fixed"),
    ]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    _, _, refute, fix1, recheck1, fix2, recheck2 = (s["prompt"] for s in seen)
    # the refuter is given only what one reviewer saw and could only read, never the reviews' own words
    assert "F2" in refute and "F3" in refute and "- F1 " not in refute and "What the steps before" not in refute
    assert "- F1 " in fix1 and "- F2 " in fix1 and "- F3 " not in fix1 and "round 1 of 2" in fix1
    assert "The fix is `git diff" in recheck1 and "fixed, not_fixed, regression" in recheck1
    assert "- F2 " in fix2 and "- F1 " not in fix2 and "round 2 of 2" in fix2
    assert "- F2 " in recheck2 and "- F1 " not in recheck2
    status = workflows.status(store, started["id"])
    assert status["state"] == "done" and status["steps"][3]["tries"] == 2
    decided = store.last_event(steps["refute"]["id"], "finished")["data"]["ledger"]
    assert decided["confirmed"] == ["F1", "F2"] and decided["refuted"] == ["F3"]
    # the session hears of the run once, at its end
    assert [t["workflow_step"] for t in store.unreported("s1")] == ["recheck"]


def test_what_the_refuter_cannot_decide_goes_to_the_person(git_repo, store, monkeypatch):
    from cauce import workflows

    results = [_reviewed("a", ("tax.py:5", "tax on the gross", "inferential", "introduced")), _reviewed("b"),
               _decides("cannot tell", F1="unsure")]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 3 and steps["refute"]["status"] == "blocked"
    stop = store.last_event(steps["refute"]["id"], "finished")["data"]["stop"]
    assert stop["cause"] == "review" and "could not decide F1" in stop["reason"]
    assert workflows.status(store, started["id"])["state"] == "waits on you"


def test_rows_still_open_after_two_rounds_go_to_the_person(git_repo, store, monkeypatch):
    results = [_reviewed("a", ("cart.py:10", "total ignores the discount", "deterministic", "introduced")),
               _reviewed("b"), ok("fixed"), _decides("no", F1="not_fixed"), ok("again"),
               _decides("still no", F1="regression")]
    _, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 6 and steps["recheck"]["status"] == "blocked"
    assert "F1 still open after 2 fix rounds" in steps["recheck"]["result"]


def test_with_no_finding_the_ledger_steps_pass_without_a_worker(git_repo, store, monkeypatch):
    from cauce import workflows

    results = [_reviewed("a", ("old.py:1", "an old leak", "deterministic", "pre-existing")), _reviewed("b")]
    started, seen, steps = _ledger_run(git_repo, store, results, monkeypatch)
    assert len(seen) == 2 and workflows.status(store, started["id"])["state"] == "done"
    assert steps["fix"]["result"] == "no confirmed finding to fix"
    assert store.last_event(steps["refute"]["id"], "finished")["data"]["skipped"]


def test_a_ledger_step_is_checked_where_it_stands():
    from cauce import workflows

    def steps(*more):
        return {"name": "l", "steps": [{"id": "r", "prompt": "Review", "kind": "review-routine"}, *more]}

    with pytest.raises(workflows.WorkflowError, match="ledger is one of"):
        workflows.normalize(steps({"id": "x", "prompt": "p", "ledger": "vote"}))
    with pytest.raises(workflows.WorkflowError, match="after the reviews"):
        workflows.normalize({"name": "l", "steps": [{"id": "x", "prompt": "p", "ledger": "refute"}]})
    with pytest.raises(workflows.WorkflowError, match="after the fix it checks"):
        workflows.normalize(steps({"id": "x", "prompt": "p", "ledger": "recheck"}))
    assert workflows.normalize(steps({"id": "f", "prompt": "p", "ledger": "fix"},
                                     {"id": "c", "prompt": "p", "ledger": "recheck"}))["steps"][2]["ledger"]


def test_the_cli_sets_a_step_s_ledger_part(git_repo, capsys, monkeypatch):
    from cauce import cli

    monkeypatch.chdir(git_repo)
    assert cli.main(["workflow", "new", "rv"]) == 0
    assert cli.main(["workflow", "step", "set", "rv", "do", "--prompt", "Review it", "--kind", "review-routine"]) == 0
    assert cli.main(["workflow", "step", "add", "rv", "decide", "--prompt", "Decide", "--ledger", "refute"]) == 0
    assert cli.main(["workflow", "step", "add", "rv", "bad", "--prompt", "x", "--ledger", "vote"]) == 1
    assert "ledger is one of refute, fix, recheck" in capsys.readouterr().err
    assert cli.main(["workflow", "step", "set", "rv", "decide", "--clear", "ledger"]) == 0
    capsys.readouterr()
    assert cli.main(["workflow", "show", "rv", "--json"]) == 0
    assert "ledger" not in json.loads(capsys.readouterr().out)["steps"][1]


def test_the_check_keeps_only_the_neighbours_that_measure(git_repo, store, verifier_on):
    from cauce import capabilities as caps

    registry = {"layout-inspector": caps.Capability("layout-inspector", {"command": "li"}),
                "rastro": caps.Capability("rastro", {"command": "rastro"})}
    script = Script(passing(), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, registry=registry, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    work, verifier = script.specs
    assert set(work.mcp_servers) == {"layout-inspector", "rastro"}
    assert set(verifier.mcp_servers) == {"layout-inspector"}
    assert "mcp__layout-inspector" in verifier.allowed_tools and "mcp__rastro" not in verifier.allowed_tools
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt


def test_a_problem_in_work_that_ran_out_of_turns_gets_more_turns_not_a_stronger_model(git_repo, store, verifier_on):
    """The palette task: the worker ran out of turns, its check passed in its place, and the
    independent check found a problem. It failed for room: same cell, twice the turns."""
    out_of_turns = WorkerResult(False, Failure.TURNS_EXHAUSTED, "used all 60 turns without a verdict",
                                cost_usd=0.1)
    script = Script(out_of_turns, check("fail", [BUG]), passing("fixed"), check("pass"), write="app.py")
    report = run("fix the cart total", git_repo, store, Options(verify="true"), registry={}, launcher=script,
                 classifier=kind("implement"))
    assert report.status == "done"
    first, _, again, _ = script.specs
    assert again.cell == first.cell and again.max_turns == first.max_turns * 2
    assert again.timeout_s == launch.timeout_for(again.max_turns)
    attempts = store.attempts(report.task_id)
    assert attempts[0]["failure"] == "turns_exhausted" and "run out of turns" in attempts[0]["summary"]
    # A turn ceiling is no dead end: only the fix that worked is remembered.
    assert [f["outcome"] for f in store.recent_problems()[0]["fixes"]] == ["worked"]
