from __future__ import annotations

from cauce import notes, repo
from cauce.escalate import Failure
from cauce.launch import WorkerResult
from cauce.orchestrate import run, verifier_brief
from cauce.store import Store

from .conftest import git
from .test_orchestrate import Script, bad, kind, ok

POLICY = "Gift card lines are never discounted, by a line rule or in the subtotal an order rule takes"


def _rule(store: Store, project: str, text: str = POLICY, *, author: str = "person") -> int:
    return notes.file_constraints(store, project, [text], author=author, source="test")[0]["id"]


def test_a_constraint_is_a_note_of_its_own_kind_with_no_anchor(store: Store):
    worker = _rule(store, "p", author="worker")
    assert store.get_note(worker)["kind"] == notes.CONSTRAINT and store.get_note(worker)["state"] == notes.CANDIDATE
    assert store.note_anchors([worker]) == []
    # The same rule again is the same note; a fact of the same words is another one.
    assert _rule(store, "p") == worker
    fact, _ = notes.add(store, "p", POLICY, topic="business", author="person", filed_by="person")
    assert fact != worker and store.get_note(fact)["kind"] is None
    assert [n["id"] for n in store.notes("p", kind="fact")] == [fact]
    assert [n["id"] for n in store.notes("p", kind=notes.CONSTRAINT)] == [worker]
    assert notes.file_constraints(store, "p", ["ok", "api key sk-abcdefghijklmnopqrstuvwx1234"], author="worker",
                                  source="test") == []


def test_constraints_are_briefed_as_requirements_as_many_as_fit(store: Store):
    for text in (POLICY, "Refunds over 500.00 need a second approver before they are paid out",
                 "Prices shown to customers always include the sales tax of their region",
                 "A customer's data is never exported to a vendor outside the region it lives in"):
        _rule(store, "p", text)
    found = notes.constraints(store, "p")
    said = notes.constraint_lines(found)
    assert said[0].startswith("Constraints this project keeps") and "must meet every one" in said[0]
    assert "never what you may run" in said[0] and len(said) == 5
    short = notes.constraint_lines(found, limit=10)
    assert len(short) == 3 and short[-1] == "- 3 more: `cauce notes list --constraints`"
    assert notes.constraint_lines([]) == []


def test_a_note_written_on_work_not_merged_is_said_to_be(git_repo, store: Store):
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "app.py").write_text("print('side')\n")
    git(git_repo, "commit", "-qam", "side")
    side = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "main")
    main = git(git_repo, "rev-parse", "HEAD")
    found = [{"id": 1, "anchors": [{"path": "app.py", "commit": side}]},
             {"id": 2, "anchors": [{"path": "app.py", "commit": main}]},
             {"id": 3, "anchors": []},
             {"id": 4, "anchors": [{"path": "app.py", "commit": "0" * 40}]}]
    marked = {n["id"]: n.get("unmerged", False) for n in notes.unmerged(found, git_repo)}
    assert marked == {1: True, 2: False, 3: False, 4: False}
    assert notes.unmerged(found, git_repo, side)[0].get("unmerged") is None
    assert "WRITTEN ON WORK NOT MERGED" in notes.line({"id": 1, "topic": "code", "title": "t", "text": "x",
                                                       "state": "current", "unmerged": True})
    assert notes.unmerged(found, None) == found


def test_every_worker_and_the_verifier_are_told_the_project_s_constraints(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")
    rule = _rule(store, repo.key(git_repo))
    script = Script(ok())
    report = run("add a loyalty discount", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), notes_ask=lambda *a, **kw: (None, 0.0, ""))
    prompt = script.specs[0].prompt
    assert "Constraints this project keeps" in prompt and POLICY in prompt
    assert "What this project's notes say" not in prompt  # a constraint is not repeated as a fact
    assert rule in store.last_event(report.task_id, "briefed")["data"][notes.BRIEFED]
    said = verifier_brief("add a loyalty discount", "implement", git_repo, "abc", ["app.py"],
                          rules=notes.constraints(store, repo.key(git_repo)))
    assert POLICY in said and "is a finding" in said
    assert "Constraints" not in verifier_brief("x", "implement", git_repo, "abc", ["app.py"])


def test_a_rule_a_failing_check_taught_is_kept_as_a_constraint(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")
    taught = WorkerResult(True, None, "done", "1 passed", constraints=(f"{POLICY} (CI: POL-7)",))
    report = run("add a loyalty discount", git_repo, store, registry={},
                 launcher=Script(bad(Failure.CODE_BUG), taught, write="app.py"), classifier=kind("implement"),
                 notes_ask=lambda *a, **kw: (None, 0.0, ""))
    assert report.status == "done"
    kept = notes.constraints(store, repo.key(git_repo))
    assert [n["text"] for n in kept] == [f"{POLICY} (CI: POL-7)"] and kept[0]["state"] == notes.CANDIDATE
    noted = [e["data"] for e in store.events_of(report.task_id, "noted") if e["data"].get("constraints")]
    assert noted and noted[0]["notes"][0]["kind"] == notes.CONSTRAINT


def test_a_rule_no_check_showed_is_kept_as_a_fact(git_repo, store: Store, monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")
    asked = []

    def ask(system, schema, question, **kw):
        asked.append(question)
        return None, 0.0, ""

    claimed = WorkerResult(True, None, "done", "1 passed", constraints=(f"{POLICY} (CI: POL-7)",))
    run("add a loyalty discount", git_repo, store, registry={}, launcher=Script(claimed, write="app.py"),
        classifier=kind("implement"), notes_ask=ask)
    assert notes.constraints(store, repo.key(git_repo)) == []
    assert asked and POLICY in asked[0]
