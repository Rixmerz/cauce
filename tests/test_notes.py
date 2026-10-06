from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cauce import cli, hooks, notes, project
from cauce.adapters.livespec import Livespec
from cauce.store import Store

from .conftest import git
from .livespec_fixture import build


def _key(where: Path) -> str:
    from cauce import repo

    return repo.key(where)


def _ask(answer: dict | None, calls: list | None = None):
    def ask(system, schema, question, **kw):
        if calls is not None:
            calls.append({"system": system, "schema": schema, "question": question, **kw})
        return answer, 0.001, "" if answer is not None else "the model did not run"
    return ask


# --- keeping and reading ------------------------------------------------------------


def test_a_note_is_kept_once_and_found_by_topic_words_and_links(store: Store):
    p = "github.com/o/shop"
    rule, new = notes.add(store, p, "A cart over ten items gets 5% off, never stacked with coupons",
                          topic="business", author="person", filed_by="person")
    assert new
    again, new = notes.add(store, p, "a cart over ten items gets 5% off, never stacked with coupons!",
                           topic="business", author="worker", filed_by="haiku")
    assert again == rule and not new  # the same fact, said again, is the same note
    code, _ = notes.add(store, p, "The volume discount is applied in total() before taxes", topic="code",
                        author="worker", filed_by="haiku", links=[(rule, "explains")])
    why, _ = notes.add(store, p, "Coupons stopped stacking in 2024: margins went negative", topic="decisions",
                       author="session", filed_by="haiku", links=[(rule, "explains")])
    other, _ = notes.add(store, "github.com/o/other", "Coupons stack freely here", topic="business",
                         author="person", filed_by="person")

    found = notes.recall(store, p, "coupons discount")
    ids = [n["id"] for n in found]
    assert other not in ids  # another project's notes are never read
    assert set(ids) == {rule, code, why}
    by_id = {n["id"]: n for n in found}
    assert by_id[rule]["why"] == "matches the question"
    assert any(x == {"to": code, "kind": "explains", "out": False} for x in by_id[rule]["links"])
    only = notes.recall(store, p, "coupons", in_topics=["decisions"], hops=0)
    assert [n["id"] for n in only] == [why]
    # one step along the links: the rule brings the code that implements it
    hop = notes.recall(store, p, "stacked ten items", hops=1, limit=1)
    assert hop[0]["id"] == rule and {n["id"] for n in hop[1:]} == {code, why}
    assert hop[1]["why"].startswith(f"linked to #{rule}")
    # nothing asked: the latest of the topic, and what they link to in other topics
    assert [n["id"] for n in notes.recall(store, p, in_topics=["code"], hops=0)] == [code]
    assert [n["id"] for n in notes.recall(store, p, in_topics=["code"])] == [code, rule]
    # a hook wants no loose matches
    assert notes.recall(store, p, "coupons are nice for christmas sales", strict=True) == []
    assert notes.recall(store, p, "do coupons stack with the ten items discount", strict=True)
    assert notes.recall(store, p, "zzz") == []


def test_replaces_retires_and_secrets_and_scraps_are_refused(store: Store):
    p = "proj"
    old, _ = notes.add(store, p, "Deploys go through the staging branch first", topic="environment",
                       author="person", filed_by="person")
    new, _ = notes.add(store, p, "Deploys go straight from main since the CI rewrite", topic="environment",
                       author="person", filed_by="person", links=[(old, "replaces")])
    assert store.get_note(old)["state"] == "replaced" and store.get_note(old)["replaced_by"] == new
    assert [n["id"] for n in notes.recall(store, p, "deploys")] == [new]
    assert notes.expand(store, [new])[0]["links"] == [{"to": old, "kind": "replaces", "out": True}]
    store.update_note(old, state="dropped")
    assert notes.expand(store, [new])[0]["links"] == []  # a note found wrong is no lead
    assert old in [n["id"] for n in notes.recall(store, p, "deploys", states=notes.STATES)]
    for bad in ("short", "the key is sk-abcdefghijklmnop", "password: hunter22 in the env"):
        with pytest.raises(ValueError):
            notes.add(store, p, bad, topic="code", author="person", filed_by="person")
    with pytest.raises(ValueError):
        notes.link(store, new, 9999, "explains")
    with pytest.raises(ValueError):
        notes.link(store, new, old, "loves")
    assert not notes.link(store, new, new, "explains")


def test_topics_are_closed_and_a_person_adds_one(store: Store):
    assert notes.resolve_topic(store, "p", "Negocio") == "business"
    assert notes.resolve_topic(store, "p", "evolución") == "decisions"
    with pytest.raises(ValueError, match="unknown topic"):
        notes.resolve_topic(store, "p", "billing")
    assert notes.add_topic(store, "p", "Billing", "invoices, taxes and payment providers") == "billing"
    assert notes.resolve_topic(store, "p", "billing") == "billing"
    assert "billing" not in notes.topics(store, "q")  # a project's own
    for name, about in (("no spaces allowed!", "x"), ("ok", " ")):
        with pytest.raises(ValueError):
            notes.add_topic(store, "p", name, about)
    assert notes.topics_in("how do we deploy the docker image") == ["environment"]
    assert notes.topics_in("¿por qué se decidió migrar?") == ["decisions"]
    assert notes.topics_in("hello there") == []


def test_the_index_is_a_few_lines_and_says_how_to_read_more(store: Store):
    assert notes.index_text(store, "p") is None
    a, _ = notes.add(store, "p", "Invoices are numbered per country, never globally", topic="business",
                     author="person", filed_by="person")
    notes.add(store, "p", "Run the suite with make test, it starts the database", topic="environment",
              author="person", filed_by="person")
    store.update_note(a, state="review", state_reason="x changed")
    text = notes.index_text(store, "p")
    assert "keeps 2 note(s)" in text and "business 1 (1 to review)" in text and "cauce recall" in text
    assert "Latest: #" in text and "one of: business, code, decisions, conventions, environment." in text
    line = notes.line(notes.expand(store, [a])[0])
    assert "(TO REVIEW: x changed)" in line


# --- anchors and review -------------------------------------------------------------------


def test_a_note_about_code_goes_to_review_when_the_code_changes(store: Store, git_repo: Path):
    key = _key(git_repo)
    (git_repo / "cart.py").write_text("def total(c): return sum(c)\n")
    (git_repo / "tax.py").write_text("RATE = 0.19\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "cart")
    about_cart, _ = notes.add(store, key, "total() sums prices before the discount", topic="code",
                              author="person", filed_by="person", paths=["cart.py"], repo_dir=git_repo)
    about_tax, _ = notes.add(store, key, "The tax rate lives in one constant", topic="code", author="person",
                             filed_by="person", paths=["./tax.py", "missing.py"], repo_dir=git_repo)
    anchors = store.note_anchors([about_tax])
    assert {a["path"] for a in anchors} == {"tax.py", "missing.py"}
    assert next(a for a in anchors if a["path"] == "missing.py")["blob"] is None
    # recall by the file a task touches
    assert [n["id"] for n in notes.recall(store, key, paths=["cart.py"])] == [about_cart]
    assert notes.recall(store, key, paths=["cart.py"])[0]["why"] == "about cart.py"

    # an unrelated commit moves the anchors along without a review
    (git_repo / "readme.md").write_text("x\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "docs")
    assert notes.review(store, key, git_repo) == []
    head = git(git_repo, "rev-parse", "HEAD")
    assert store.note_anchors([about_cart])[0]["commit_sha"] == head

    (git_repo / "cart.py").write_text("def total(c): return sum(c) * 0.95\n")
    (git_repo / "tax.py").unlink()
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "change")
    flagged = dict(notes.review(store, key, git_repo))
    assert "cart.py changed since this note was written" in flagged[about_cart]
    assert "tax.py was deleted" in flagged[about_tax]
    assert store.get_note(about_cart)["state"] == "review"
    assert notes.review(store, key, git_repo) == []  # once
    notes.confirm(store, about_cart, git_repo)
    assert store.get_note(about_cart)["state"] == "current"
    assert store.note_anchors([about_cart])[0]["commit_sha"] == git(git_repo, "rev-parse", "HEAD")
    assert notes.review(store, key, git_repo) == []
    assert notes.confirm(store, 999, git_repo) is None
    assert notes.review(store, key, git_repo.parent) == []  # no checkout, nothing to compare


def test_work_on_an_unmerged_branch_is_not_a_change_until_it_lands(store: Store, git_repo: Path):
    key = _key(git_repo)
    git(git_repo, "checkout", "-qb", "cauce/task-1")
    (git_repo / "app.py").write_text("print('new')\n")
    git(git_repo, "commit", "-qam", "branch work")
    branch_head = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "main")
    note, _ = notes.add(store, key, "app.py prints the new greeting", topic="code", author="worker",
                        filed_by="haiku", paths=["app.py"], repo_dir=git_repo, commit=branch_head)
    assert notes.review(store, key, git_repo) == []  # main does not have it yet: nothing to review
    # squash-merged: the content lands without the commit; the anchor follows it
    (git_repo / "app.py").write_text("print('new')\n")
    git(git_repo, "commit", "-qam", "squash")
    assert notes.review(store, key, git_repo) == []
    assert store.note_anchors([note])[0]["commit_sha"] == git(git_repo, "rev-parse", "HEAD")
    assert notes.anchor(store, note, git_repo) == 0
    outside = git_repo.parent / "plain"
    outside.mkdir()
    assert notes.anchor(store, note, outside, ["x.txt"]) == 1
    assert store.note_anchors([note])[-1]["commit_sha"] is None


def test_a_fact_naming_a_symbol_is_anchored_to_it_read_only(store: Store, git_repo: Path, tmp_path):
    db = build(git_repo, project_root=git_repo.resolve())
    before = db.read_bytes()
    adapter = Livespec(env={"PATH": "", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
                            "CAUCE_HOME": str(tmp_path / "h")})
    status = adapter.inspect(git_repo)
    found = adapter.symbols_named("charge_card retries are idempotent; test_parse_amount covers it", status)
    assert found == [("src.billing.charge_card", "src/billing.py")]
    assert adapter.symbols_named("nothing here", status) == []
    from cauce.adapters import Status

    assert adapter.symbols_named("charge_card", Status("livespec", "absent")) == []
    assert db.read_bytes() == before
    # through the setting: off gives no lookup; on and indexed gives one
    assert notes.livespec_symbols(git_repo) is None
    env = {"CAUCE_LIVESPEC": "on", "PATH": "", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc"),
           "CAUCE_HOME": str(tmp_path / "h")}
    lookup = notes.livespec_symbols(git_repo, env)
    assert lookup("charge_card") == [("src.billing.charge_card", "src/billing.py")]
    assert notes.livespec_symbols(tmp_path, env) is None


# --- filing with Haiku ------------------------------------------------------------------


def test_haiku_files_each_fact_under_a_topic_with_links_and_anchors(store: Store, git_repo: Path):
    key = _key(git_repo)
    rule, _ = notes.add(store, key, "Refunds are only allowed within 30 days of purchase", topic="business",
                        author="person", filed_by="person")
    answer = {"notes": [
        {"fact": 0, "keep": True, "topic": "code", "title": "Refund window check", "duplicate_of": None,
         "links": [{"to": rule, "kind": "explains"}, {"to": 4242, "kind": "explains"}],
         "paths": ["app.py", "not-touched.py"], "new_topic": "refunds"},
        {"fact": 1, "keep": False, "topic": "code", "title": "x", "duplicate_of": None, "links": [], "paths": [],
         "new_topic": None},
        {"fact": 2, "keep": True, "topic": "business", "title": "same", "duplicate_of": rule, "links": [],
         "paths": ["app.py"], "new_topic": None},
        {"fact": 9, "keep": True, "topic": "code", "title": "out of range", "duplicate_of": None, "links": [],
         "paths": [], "new_topic": None},
    ]}
    calls: list = []
    filed = notes.file_facts(store, key, ["refund_window() in app.py enforces the 30 day rule",
                                          "I edited app.py", "Refunds only within thirty days",
                                          "x", "Prices are stored in cents everywhere"],
                             author="worker", source="task #3", task_id=3, repo_dir=git_repo,
                             paths=["app.py"], ask=_ask(answer, calls),
                             symbols_of=lambda f: [("src.refund_window", "app.py")] if "refund_window" in f else [])
    assert [f["new"] for f in filed] == [True, False, True]
    first = store.get_note(filed[0]["id"])
    assert (first["topic"], first["title"], first["filed_by"], first["proposed_topic"]) == (
        "code", "Refund window check", "haiku", "refunds")
    assert first["source"] == "task #3" and first["task_id"] == 3
    expanded = notes.expand(store, [first["id"]])[0]
    assert expanded["links"] == [{"to": rule, "kind": "explains", "out": True}]  # an invented id is dropped
    assert {(a["path"], a["symbol"]) for a in expanded["anchors"]} == {
        ("app.py", None), ("app.py", "src.refund_window")}
    assert filed[1]["id"] == rule and store.note_anchors([rule])[0]["path"] == "app.py"
    # a fact Haiku did not answer for keeps the topic its words point to
    assert filed[2]["topic"] == "business" and store.get_note(filed[2]["id"])["filed_by"] == "rule"
    asked = calls[0]
    assert "Existing notes:" in asked["question"] and f"#{rule} [business]" in asked["question"]
    assert "- app.py" in asked["question"] and "3. Prices are stored in cents" in asked["question"]
    assert asked["schema"]["properties"]["notes"]["items"]["properties"]["topic"]["enum"][0] == "business"


def test_without_haiku_a_fact_is_still_kept_by_its_words(store: Store):
    filed = notes.file_facts(store, "p", ["The docker image is built by make image", "tiny"],
                             author="worker", source="task #1", ask=_ask(None))
    assert len(filed) == 1 and filed[0]["topic"] == "environment"
    assert store.get_note(filed[0]["id"])["filed_by"] == "rule"
    assert notes.file_facts(store, "p", [], author="worker", source="x", ask=_ask(None)) == []


# --- what a session said ---------------------------------------------------------------


def _transcript(path: Path, lines: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as out:
        for line in lines:
            out.write(json.dumps(line) + "\n")


def _said(role: str, text, **extra) -> dict:
    return {"type": role, "message": {"role": role, "content": text}, **extra}


def test_a_session_s_conversation_is_read_once_without_tools_or_injected_context(store: Store, git_repo, tmp_path):
    key = _key(git_repo)
    transcript = tmp_path / "s.jsonl"
    long = "Invoices are numbered per country in app.py and the sequence never resets, the tax office says. " * 8
    _transcript(transcript, [
        _said("user", "<command-name>/clear</command-name>"),
        _said("user", f"{long}<system-reminder>secret context</system-reminder>"),
        _said("assistant", [{"type": "text", "text": "Understood: per-country sequences."},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
        _said("user", [{"type": "tool_result", "content": "tool output that must not be read"}]),
        _said("user", "side chain", isSidechain=True),
        {"type": "summary", "summary": "x"},
        "not a dict",
    ])
    text, end = notes.conversation(transcript, 0)
    assert "Person: Invoices" in text and "Agent: Understood" in text
    assert "secret context" not in text and "tool output" not in text and "side chain" not in text
    assert "/clear" not in text and end == transcript.stat().st_size
    assert notes.conversation(tmp_path / "missing.jsonl", 0) == ("", 0)

    calls: list = []
    answer = {"facts": [
        {"text": "Invoices are numbered per country; the sequence never resets (tax office rule)",
         "topic": "business", "title": "Invoice numbering", "duplicate_of": None, "links": [],
         "paths": ["app.py", "nope.py", "other.py"], "new_topic": None},
        {"text": "token ghp_abcdefghijklmnopqrstuvwxyz", "topic": "code", "title": "x", "duplicate_of": None,
         "links": [], "paths": [], "new_topic": None},
        "not a dict",
    ]}
    (git_repo / "other.py").write_text("exists, but the conversation never names it\n")
    filed = notes.extract(store, key, "sess", transcript, repo_dir=git_repo, ask=_ask(answer, calls))
    assert len(filed) == 1
    note = store.get_note(filed[0]["id"])
    assert (note["author"], note["session_id"], note["source"]) == ("session", "sess", "session sess")
    assert [a["path"] for a in store.note_anchors([note["id"]])] == ["app.py"]  # named, and there
    assert "Conversation:" in calls[0]["question"]
    # read once: nothing new, no call
    assert notes.extract(store, key, "sess", transcript, ask=_ask(answer, calls)) == [] and len(calls) == 1
    # a failed call leaves the offset where it was, so the next one reads it again
    _transcript(transcript, [_said("user", long)])
    assert notes.extract(store, key, "sess", transcript, ask=_ask(None, calls)) == []
    assert notes.extract(store, key, "sess", transcript, ask=_ask({"facts": []}, calls)) == []
    assert len(calls) == 3
    # too little said is not worth a call yet: it is read with what follows
    _transcript(transcript, [_said("user", "ok thanks"), _said("user", "summary", isCompactSummary=True)])
    assert notes.extract(store, key, "sess", transcript, ask=_ask(answer, calls)) == [] and len(calls) == 3
    _transcript(transcript, [_said("user", "Shipping is free above 50000 pesos, metro region only.")])
    notes.extract(store, key, "sess", transcript, ask=_ask({"facts": []}, calls))
    assert len(calls) == 4 and "ok thanks" in calls[-1]["question"] and "summary" not in calls[-1]["question"]


# --- the command line -----------------------------------------------------------------


def test_the_commands_keep_read_review_and_tidy_notes(git_repo: Path, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    assert cli.main(["note", "Orders over 100 USD need a manager approval", "--topic", "negocio",
                     "--anchor", "app.py"]) == 0
    assert "kept #1 [business]" in capsys.readouterr().out
    assert cli.main(["note", "approve_order() in app.py checks the 100 USD threshold", "--link", "1:explains"]) == 0
    assert "kept #2 [code]" in capsys.readouterr().out
    assert cli.main(["note", "Orders over 100 USD need a manager approval", "--topic", "business"]) == 0
    assert "already known as #1" in capsys.readouterr().out
    assert cli.main(["note", "x", "--topic", "business"]) == 2
    assert cli.main(["note", "A fact long enough to keep", "--topic", "nope"]) == 2
    capsys.readouterr()

    assert cli.main(["recall", "manager approval"]) == 0
    out = capsys.readouterr().out
    assert "#1 [business] Orders over 100 USD" in out and "links: #2 explains this" in out and "about: app.py" in out
    assert cli.main(["recall", "--path", "app.py", "--json"]) == 0
    assert [n["id"] for n in json.loads(capsys.readouterr().out)] == [1, 2]
    assert cli.main(["recall", "approval", "--topic", "code", "--hops", "0"]) == 0
    assert "#1" not in capsys.readouterr().out
    assert cli.main(["recall", "x", "--topic", "nope"]) == 2
    assert cli.main(["recall", "zzz"]) == 0 and "no notes match" in capsys.readouterr().out

    assert cli.main(["notes", "topics"]) == 0
    assert "business" in capsys.readouterr().out
    assert cli.main(["notes", "topic", "add", "approvals", "who approves what"]) == 0
    assert cli.main(["notes", "move", "1", "approvals"]) == 0
    assert cli.main(["notes", "link", "2", "1", "example_of"]) == 0
    assert cli.main(["notes", "show", "2"]) == 0
    assert "example_of #1" in capsys.readouterr().out
    assert cli.main(["notes", "unlink", "2", "1"]) == 0
    assert "2 link(s)" in capsys.readouterr().out
    assert cli.main(["notes", "show", "77"]) == 1 and cli.main(["notes", "ok", "77"]) == 1

    (git_repo / "app.py").write_text("print('changed')\n")
    git(git_repo, "commit", "-qam", "change")
    assert cli.main(["notes", "list", "--review"]) == 0
    assert "app.py changed since this note was written" in capsys.readouterr().out
    assert cli.main(["notes", "ok", "1"]) == 0
    assert cli.main(["notes", "drop", "2", "--why", "wrong"]) == 0
    capsys.readouterr()
    assert cli.main(["notes"]) == 0
    out = capsys.readouterr().out
    assert "#1" in out and "#2" not in out
    assert cli.main(["notes", "list", "--all-states", "--json"]) == 0
    assert {n["state"] for n in json.loads(capsys.readouterr().out)} == {"current", "dropped"}
    assert cli.main(["notes", "list", "--topic", "nope"]) == 2


def test_extract_notes_runs_once_per_session(git_repo: Path, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "t.jsonl"
    said = "The staging database is reset every night at 3am, never rely on it. " * 12
    _transcript(transcript, [_said("user", said)])
    answer = {"facts": [{"text": "The staging database is reset every night at 3am", "topic": "environment",
                         "title": "Staging resets nightly", "duplicate_of": None, "links": [], "paths": [],
                         "new_topic": None}]}
    monkeypatch.setattr("cauce.classify.ask_haiku", _ask(answer))
    assert cli.main(["extract-notes", "s1", str(transcript), str(git_repo)]) == 0
    assert "kept #1 [environment] Staging resets nightly" in capsys.readouterr().out
    from cauce import dispatch
    from cauce.store import home

    with dispatch.hold(home(), "notes:s2"):
        assert cli.main(["extract-notes", "s2", str(transcript), str(git_repo)]) == 0
    assert capsys.readouterr().out == ""


# --- the hooks ----------------------------------------------------------------------------


def test_sessions_get_the_index_matching_notes_and_keep_what_they_said(store: Store, git_repo: Path, tmp_path,
                                                                        monkeypatch):
    key = _key(git_repo)
    notes.add(store, key, "Every price is stored in cents as an integer", topic="business", author="person",
              filed_by="person")
    start = {"session_id": "s", "cwd": str(git_repo), "source": "compact"}
    assert hooks.session_start(start, store, tmp_path / "root", {}, adapters=[]) is None  # notes are off
    monkeypatch.setenv("CAUCE_NOTES", "on")
    assert hooks.session_start(start, store, tmp_path / "root", {}, adapters=[]) is None  # not enrolled
    project.enroll(git_repo)
    context = hooks.session_start(start, store, tmp_path / "root", {}, adapters=[])["hookSpecificOutput"]
    assert "cauce notes: this project keeps 1 note(s)" in context["additionalContext"]

    prompt = {"session_id": "s", "prompt_id": "p1", "cwd": str(git_repo),
              "prompt": "why is every price stored in cents in the checkout"}
    added = hooks.user_prompt_submit(prompt, store)["hookSpecificOutput"]["additionalContext"]
    assert "Every price is stored in cents" in added
    task = store.list_tasks()[0]
    assert store.last_event(task["id"], "notes_shown")["data"]["ids"] == [1]
    assert hooks.user_prompt_submit({**prompt, "prompt_id": "p2", "prompt": "rename the button"}, store) is None

    started = []
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    event = {"session_id": "s", "transcript_path": str(transcript), "cwd": str(git_repo)}
    popen = lambda argv, **kw: started.append((argv, kw))  # noqa: E731
    env = {**__import__("os").environ, "CAUCE_NOTES": "on"}
    assert hooks.keep_notes(event, tmp_path / "root", env, popen=popen)
    argv, kw = started[0]
    assert argv[1:] == ["-m", "cauce", "extract-notes", "s", str(transcript), str(git_repo)]
    assert kw["start_new_session"] and "CAUCE_SESSION_ID" not in kw["env"]
    assert not hooks.keep_notes(event, tmp_path / "root", {**env, "CAUCE_NOTES": "off"}, popen=popen)
    assert not hooks.keep_notes({**event, "transcript_path": str(tmp_path / "none")}, tmp_path / "root", env,
                                popen=popen)
    assert not hooks.keep_notes({**event, "cwd": str(tmp_path)}, tmp_path / "root", env, popen=popen)

    def broken(*a, **kw):
        raise OSError("no fork")

    assert not hooks.keep_notes(event, tmp_path / "root", env, popen=broken)
    from cauce import dispatch

    with dispatch.hold(tmp_path / "root", "notes:s"):
        assert not hooks.keep_notes(event, tmp_path / "root", env, popen=popen)
    # the hook entry point: PreCompact and SessionEnd answer nothing, and start the extraction
    calls = []
    monkeypatch.setattr(hooks, "keep_notes", lambda event, root, env: calls.append(event["session_id"]))
    for name in ("PreCompact", "SessionEnd"):
        assert hooks.handle(name, event, store, tmp_path / "root", env) is None
    assert calls == ["s", "s"]


def test_the_plugin_declares_the_new_hooks_and_the_setting():
    root = Path(__file__).resolve().parents[1]
    declared = json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]
    assert {"PreCompact", "SessionEnd"} <= set(declared)
    options = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    assert options["notes"]["default"] is True


def test_the_hook_process_answers_precompact_with_nothing(tmp_path, git_repo):
    env = {"CAUCE_HOME": str(tmp_path / "h"), "CAUCE_NOTES": "off", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    event = json.dumps({"session_id": "s", "transcript_path": "/nope", "cwd": str(git_repo)})
    proc = subprocess.run([sys.executable, "-m", "cauce", "hook", "PreCompact"], input=event, capture_output=True,
                          text=True, env=env, check=False)
    assert proc.returncode == 0 and proc.stdout == ""
