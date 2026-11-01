"""Branches of `locks.notes` the notes suite leaves unread."""
from __future__ import annotations

import json
from pathlib import Path

from locks import notes
from locks.store import Store

from .conftest import git
from .test_learning import _briefed
from .test_notes import _ask, _key
from .test_standards import finished


def test_a_security_alert_skips_an_empty_finding_and_says_one_thing_once():
    found = notes.security_alerts(
        ["The AWS credentials were committed in config.py", "the aws credentials were  committed in config.py"],
        [{"where": "x.py", "what": ""}, {"where": "", "what": "an injection in the search query"}])
    assert found == ["The AWS credentials were committed in config.py",
                     "the aws credentials were committed in config.py", "an injection in the search query"]
    assert notes.security_alerts(["a leaked api key", "a leaked api key"]) == ["a leaked api key"]


def test_no_paths_have_no_blobs_and_a_path_of_no_member_is_in_no_checkout(tmp_path: Path):
    assert notes._blobs(tmp_path, "HEAD", []) == {}
    api = tmp_path / "api"
    assert notes._placed(False, [("api/", api)], "web/x.py") == ("web/x.py", None, "web/x.py")
    assert notes._placed(False, [("api/", api)], "api/x.py") == ("api/x.py", api, "x.py")


def test_a_checkout_with_no_commit_reviews_nothing(store: Store, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    note = store.add_note(str(empty.resolve()), "code", "Cart", "The cart rounds once.", author="person",
                          filed_by="person")
    store.add_anchor(note, "cart.py", commit_sha="a" * 40, blob="b" * 40)
    assert notes.review(store, str(empty.resolve()), empty) == []
    assert store.get_note(note)["state"] == "current"


def test_confirming_leaves_an_anchor_no_checkout_here_holds(store: Store, tmp_path: Path):
    note = store.add_note("p", "code", "Cart", "The cart rounds once.", author="person", filed_by="person")
    store.add_anchor(note, "cart.py", commit_sha="a" * 40, blob="b" * 40)
    store.update_note(note, state="review", state_reason="cart.py changed")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert notes.confirm(store, note, plain)["state"] == "current"
    (anchor,) = store.note_anchors([note])
    assert (anchor["commit_sha"], anchor["blob"]) == ("a" * 40, "b" * 40)
    # With no directory at all, the note is current again and its anchors stay as they were.
    store.update_note(note, state="review", state_reason="cart.py changed")
    assert notes.confirm(store, note, None)["state"] == "current"
    assert store.note_anchors([note])[0]["blob"] == "b" * 40
    assert notes.confirm(store, 4242, None) is None


def test_a_task_s_first_brief_is_the_one_its_notes_are_read_from(store: Store):
    task = store.create_task("t", status="done", source="locks", repo="r", kind="implement")
    _briefed(store, task["id"], carried=[1, 2], withheld=[3])
    _briefed(store, task["id"], carried=[9], withheld=[9])
    other = store.create_task("u", status="done", source="locks", repo="r", kind="implement")
    store.add_event(other["id"], "briefed", session=None)  # a brief that kept no list
    assert notes.carried(store) == {task["id"]: {1, 2}}
    assert notes.withheld(store) == {task["id"]: {3}}


def test_with_no_repository_the_recall_order_is_fused():
    assert notes.recall_arm(None, None, 1) == ("fused", "no repository to run a trial in")


def test_a_candidate_every_reader_failed_is_dropped_under_the_trial_too(store: Store, monkeypatch):
    monkeypatch.setenv("LOCKS_NOTE_TRIALS", "on")
    lone = store.add_note("r", "code", "t", "The cache is warmed by a cron job", author="worker", filed_by="haiku",
                          state=notes.CANDIDATE)
    for _ in range(notes.DISPROVE_AFTER):
        _briefed(store, finished(store, ("sonnet/medium", 0, "code_bug", "more_effort"), ("sonnet/high", 0)),
                 carried=[lone])
    assert notes.weigh(store) == {"current": [], "dropped": [lone]}
    assert store.get_note(lone)["state_reason"].startswith("unproven: in the brief of")


def test_a_note_both_anchored_and_asked_about_says_both(store: Store):
    note = store.add_note("p", "code", "Cart rounding", "The cart total rounds once at checkout", author="person",
                          filed_by="person")
    store.add_anchor(note, "cart.py")
    (found,) = notes.recall(store, "p", "cart total rounds", paths=["cart.py"], hops=0)
    assert found["why"] == "about cart.py and matches the question"


def test_the_links_one_step_away_stop_at_their_share(store: Store):
    hub = store.add_note("p", "code", "Hub", "The pricing engine computes totals", author="person",
                         filed_by="person")
    spokes = [store.add_note("p", "code", f"Spoke {n}", f"Detail number {n} of somewhere else entirely",
                             author="person", filed_by="person") for n in range(4)]
    for spoke in spokes:
        notes.link(store, spoke, hub, "explains")
    found = notes.recall(store, "p", "pricing engine computes totals", limit=2, hops=1)
    linked = [n for n in found if n["why"].startswith("linked to")]
    assert found[0]["id"] == hub and len(linked) == 2  # max(2, limit // 2) steps, no more


def test_a_note_line_names_its_links_both_ways(store: Store):
    a = store.add_note("p", "code", "A", "The cart rounds once at checkout", author="person", filed_by="person")
    b = store.add_note("p", "code", "B", "Rounding happens in total()", author="person", filed_by="person")
    notes.link(store, b, a, "explains")
    line_a = notes.line(notes.expand(store, [a])[0])
    line_b = notes.line(notes.expand(store, [b])[0])
    assert line_a.endswith(f" · #{b} explains this")
    assert line_b.endswith(f" · explains #{a}")


def test_a_constraint_the_store_refuses_is_skipped(store: Store, monkeypatch):
    real = notes.add

    def add(store, project, text, **kw):
        if "refused" in text:
            raise ValueError("refused")
        return real(store, project, text, **kw)

    monkeypatch.setattr(notes, "add", add)
    kept = notes.file_constraints(store, "p", ["This one is refused by the store", "Gift cards are never discounted"],
                                  author="person", source="t")
    assert [k["title"] for k in kept] == [store.get_note(kept[0]["id"])["title"]]
    assert len(kept) == 1 and "Gift cards" in store.get_note(kept[0]["id"])["text"]


def test_no_notes_make_no_brief_and_an_index_can_leave_the_latest_out(store: Store):
    assert notes.brief_lines([]) == []
    store.add_note("p", "code", "Cart", "The cart rounds once.", author="person", filed_by="person")
    text = notes.index_text(store, "p", latest=0)
    assert text.startswith("locks notes: this project keeps 1 note(s)") and "Latest:" not in text


def test_a_duplicate_haiku_names_takes_its_links_without_a_checkout(store: Store, git_repo: Path):
    key = _key(git_repo)
    rule, _ = notes.add(store, key, "Refunds are only allowed within 30 days of purchase", topic="business",
                        author="person", filed_by="person")
    why, _ = notes.add(store, key, "Refund abuse cost a lot of money in the past", topic="decisions",
                       author="person", filed_by="person")
    answer = {"notes": [{"fact": 0, "keep": True, "topic": "business", "title": "same", "duplicate_of": rule,
                         "links": [{"to": why, "kind": "explains"}], "paths": [], "new_topic": None}]}
    filed = notes.file_facts(store, key, ["Refunds only within thirty days of purchase"], author="worker",
                             source="task #3", ask=_ask(answer))
    assert filed == [{"id": rule, "new": False, "topic": "business", "title": store.get_note(rule)["title"]}]
    assert notes.expand(store, [rule])[0]["links"] == [{"to": why, "kind": "explains", "out": True}]
    assert store.note_anchors([rule]) == []


def test_a_fact_the_store_refuses_files_nothing(store: Store, monkeypatch):
    def refuse(*a, **kw):
        raise ValueError("refused")

    monkeypatch.setattr(notes, "add", refuse)
    assert notes.file_facts(store, "p", ["Prices are stored in cents everywhere"], author="worker", source="t",
                            ask=_ask(None)) == []


def test_a_message_whose_content_is_neither_text_nor_blocks_says_nothing(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    lines = [{"type": "user", "message": {"content": {"odd": "shape"}}},
             {"type": "user", "message": {"content": "Keep prices in cents"}}]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    said, end = notes.conversation(path, 0)
    assert said == "Person: Keep prices in cents"
    assert end == path.stat().st_size
