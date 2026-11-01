"""Branches of `cauce.revisions` and `cauce.merging` their suites leave unread."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cauce import classify, merging, revisions
from cauce.store import Store

from .conftest import git
from .test_revisions import LESSONS, Editor, _candidate, _evidence, _task

# --- revisions -----------------------------------------------------------------------------------


def test_no_lessons_make_no_brief_text():
    assert revisions.text_for("implementer", [], on_trial=True) == ""


def test_a_task_with_no_attempt_or_a_first_try_that_said_nothing_is_not_measured(store: Store):
    none = _task(store, first=None)
    env = store.create_task("t", status="blocked", source="cauce", repo="r")
    store.add_attempt(env["id"], cell="sonnet/medium", max_turns=80, passed=0, failure="environment")
    counted = _task(store, first=True)
    found = revisions.measure(store, [none, env["id"], counted])
    assert found["tasks"] == 1 and found["passed_first"] == 1


def test_two_rounds_of_the_check_in_a_row_are_a_correction_the_editor_reads(store: Store):
    task = _task(store, blockers=1)
    store.add_event(task, "verified", passed=False, blocking=1, blockers=[{"where": "", "what": "still drops it"}])
    met = revisions.evidence(store, "r", "implementer")
    assert met["corrections"] == [f"task #{task} took 2 rounds of the independent check in a row"]
    assert "still drops it" in met["blockers"]


def test_without_an_ask_the_editor_is_haiku(store: Store, monkeypatch):
    _evidence(store, n=3)
    editor = Editor()
    monkeypatch.setattr(classify, "ask_haiku", editor)
    made = revisions.propose(store, "r", "implementer")
    assert made["lessons"] == LESSONS and len(editor.asked) == 1


def test_a_candidate_another_settle_proposed_meanwhile_wins(store: Store, monkeypatch):
    _evidence(store, n=3)
    real = revisions.state
    calls = []

    def state(store, repo, role):
        calls.append(1)
        found = real(store, repo, role)
        if len(calls) > 2:  # read again under the write lock: another proposal landed
            return {**found, "candidate": {"id": 99}}
        return found

    monkeypatch.setattr(revisions, "state", state)
    assert revisions.propose(store, "r", "implementer", ask=Editor()) is None
    assert store.role_revisions(repo="r", role="implementer") == []


def test_settle_reads_endings_another_settle_took_meanwhile_as_none(store: Store, monkeypatch):
    monkeypatch.setenv("CAUCE_ROLE_TRIALS", "on")
    _task(store)
    real = store.endings_after
    calls = []

    def endings_after(after):
        calls.append(after)
        return real(after) if len(calls) == 1 else []

    monkeypatch.setattr(store, "endings_after", endings_after)
    assert revisions.settle(store, ask=Editor()) == {"judged": [], "proposed": []}


def test_settle_skips_endings_of_no_repository_an_investigator_or_no_role_and_a_proposal_that_is_none(
        store: Store, monkeypatch):
    monkeypatch.setenv("CAUCE_ROLE_TRIALS", "on")
    lone = store.create_task("t", status="done", source="cauce")
    store.add_event(lone["id"], "finished", status="done")
    probe = store.create_task("t", status="done", source="queue", repo="r", options='{"investigation": "i1"}')
    store.add_event(probe["id"], "finished", status="done")
    _task(store, role="verifier")
    _evidence(store, n=3)
    _task(store)
    editor = Editor(answer="not a dict")  # the editor gives nothing that reads
    found = revisions.settle(store, ask=editor)
    assert found == {"judged": [], "proposed": []}
    assert len(editor.asked) == 1  # asked for the implementer alone


def test_a_role_with_lessons_and_no_candidate_lists_no_trial(store: Store):
    cand = _candidate(store)
    store.add_role_revision(at="2026-10-22T00:00:00+00:00", repo="r", role="implementer", record="adopted",
                            revision=cand["id"], hash=revisions.lessons_hash(LESSONS), lessons=LESSONS,
                            reason="won", version="t")
    lines = revisions.text(revisions.overview(store, "r"))
    assert lines[0].startswith(f"implementer in r: revision #{cand['id']}")
    assert not any("on trial" in line for line in lines)


# --- merging -------------------------------------------------------------------------------------


def _branch(repo: Path, name: str, files: dict[str, str]) -> None:
    git(repo, "checkout", "-q", "-b", name)
    for path, body in files.items():
        (repo / path).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "checkout", "-q", "main")


def test_a_detached_checkout_or_a_branch_already_gone_takes_no_merge(git_repo: Path):
    _branch(git_repo, "cauce/task-1", {"a.txt": "a\n"})
    with pytest.raises(merging.MergeError, match="no kept branch for #7"):
        merging.ready(git_repo, {1: "cauce/task-1", 7: "cauce/task-7"})
    git(git_repo, "checkout", "-q", "--detach")
    with pytest.raises(merging.MergeError, match="detached HEAD"):
        merging.ready(git_repo, {1: "cauce/task-1"})


def test_merges_with_no_check_onto_a_branch_that_exists_and_a_check_that_hangs(git_repo: Path, monkeypatch):
    _branch(git_repo, "cauce/task-1", {"a.txt": "a\n"})
    _branch(git_repo, "cauce/task-2", {"b.txt": "b\n"})
    git(git_repo, "branch", "taken")
    with pytest.raises(merging.MergeError, match="could not make branch taken"):
        merging.merge(git_repo, {1: "cauce/task-1"}, {1: "a"}, {1: ["a.txt"]}, onto="taken")
    done = merging.merge(git_repo, {1: "cauce/task-1", 2: "cauce/task-2"}, {1: "a", 2: "b"},
                         {1: ["a.txt"], 2: ["b.txt"]})
    assert done.merged == [1, 2] and done.failed is None and done.base == ""
    merging.back(git_repo, done)  # nothing went onto a branch of its own: nothing to check out again
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    _branch(git_repo, "cauce/task-3", {"c.txt": "c\n"})
    monkeypatch.setattr(merging, "CHECK_TIMEOUT_S", 0.2)
    done = merging.merge(git_repo, {3: "cauce/task-3"}, {3: "c"}, {3: ["c.txt"]}, verify="sleep 5")
    assert done.failed == (3, "the check ran past 0.2s")


def test_a_pull_request_body_of_a_bare_task_and_one_of_many_files():
    bare = merging.pr_body({"id": 4, "title": "fix  the cart"}, {})
    assert bare == "cauce task #4: fix the cart\n"
    many = merging.pr_body({"id": 5, "title": "t", "result": "Done."}, {"changed": [f"f{n}.py" for n in range(43)]})
    assert "- `f39.py`" in many and "- `f40.py`" not in many and many.endswith("- and 3 more\n")


def test_a_pull_request_names_its_base_and_says_when_gh_cannot_run(git_repo: Path):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        if argv[0] == "gh":
            raise OSError("gh: not found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    opened, said = merging.open_pr(git_repo, "cauce/task-1", "t", "b", base="main", run=run)
    assert (opened, said) == (False, "gh did not run: gh: not found")
    assert seen[1][-2:] == ["--base", "main"]
