from __future__ import annotations

import json
from pathlib import Path

from cauce import compact, hooks
from cauce.store import Store


def user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def transcript(tmp_path: Path, entries) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return path


ENTRIES = [
    user("fix the login redirect, keep `?next=`"),
    user("<command-name>/model</command-name>"),
    user("Caveat: harness text"),
    user("summary", isCompactSummary=True),
    user([{"type": "text", "text": "meta"}], isMeta=True),
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Edit", "input": {"file_path": "/r/app.py"}},
        {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm test"}},
        {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "rm -rf x"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b", "is_error": True,
         "content": [{"type": "text", "text": "Exit code 1\nTypeError: x is undefined"}]},
        {"type": "tool_result", "tool_use_id": "c", "is_error": True,
         "content": "The user doesn't want to proceed with this tool use."},
    ]}},
    user([{"type": "text", "text": "and add a test"}]),
]


def test_read_keeps_what_the_person_typed_and_what_broke(tmp_path):
    facts = compact.read(transcript(tmp_path, ENTRIES))
    assert facts["requests"] == ["fix the login redirect, keep `?next=`", "and add a test"]
    assert facts["written"] == ["/r/app.py"]
    assert facts["failures"] == ["Bash `npm test`: Exit code 1 TypeError: x is undefined"]


def test_digest_fits_its_budget_and_keeps_the_newest_requests():
    facts = {"requests": [f"request {i} " + "x" * 300 for i in range(40)], "written": [], "failures": []}
    text = compact.digest(facts, changed=["a.py"], tasks=[{"id": 3, "status": "done", "title": "t"}], budget=3000)
    assert len(text) <= 3000
    assert "request 39" in text and "request 0 " not in text and "earlier left out" in text
    assert "- a.py" in text and "#3 [done] t" in text


def test_nothing_to_keep_is_no_digest():
    assert compact.digest({"requests": [], "written": [], "failures": []}, changed=[], tasks=[]) == ""


def test_an_id_that_is_no_file_name_is_refused(tmp_path):
    assert compact.path(tmp_path, "../x") is None
    assert compact.recall(tmp_path, "../x") is None
    assert compact.keep(tmp_path, "../x", tmp_path / "t.jsonl", None, []) is None


def test_precompact_keeps_and_the_session_after_it_gets_it_back(store: Store, tmp_path, git_repo):
    (git_repo / "new.py").write_text("x")
    event = {"session_id": "s1", "transcript_path": str(transcript(tmp_path, ENTRIES)), "cwd": str(git_repo)}
    hooks.handle("PreCompact", event, store, tmp_path, {})
    kept = compact.recall(tmp_path, "s1")
    assert kept and "fix the login redirect" in kept and "- new.py" in kept
    answer = hooks.session_start({"session_id": "s1", "source": "compact", "cwd": str(git_repo)}, store,
                                 tmp_path, {})
    assert "cauce compact:" in answer["hookSpecificOutput"]["additionalContext"]
    fresh = hooks.session_start({"session_id": "s1", "source": "startup", "cwd": str(git_repo)}, store,
                                tmp_path, {})
    assert fresh is None or "cauce compact:" not in fresh["hookSpecificOutput"]["additionalContext"]


def test_no_transcript_keeps_nothing(store: Store, tmp_path):
    assert hooks.keep_digest({"session_id": "s1"}, store, tmp_path) is None
    assert hooks.keep_digest({"session_id": "s1", "transcript_path": str(tmp_path / "none")}, store,
                             tmp_path) is None


def test_git_that_cannot_run_is_no_change(tmp_path):
    assert compact._changed(tmp_path / "missing") == []


def test_a_secret_is_masked():
    facts = {"requests": ["use password=hunter2 for it"], "written": [], "failures": []}
    text = compact.digest(facts, changed=[], tasks=[])
    assert "hunter2" not in text and "[secret]" in text
