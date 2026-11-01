"""Branches of `cauce.compaction` and `cauce.compact` their suites leave unread."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cauce import compact, compaction
from cauce.store import Store, home

from .test_compaction import assistant, big, turn, write

# --- compaction ----------------------------------------------------------------------------------


def test_a_time_that_does_not_read_is_none():
    assert compaction._stamp("yesterday at noon") is None
    assert compaction._stamp(3) is None


def test_a_cache_write_split_that_names_no_lifetime_leaves_it_unknown(tmp_path: Path):
    at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    path = write(tmp_path, [assistant(150_000, at, split={"ephemeral_1h_input_tokens": 0,
                                                          "ephemeral_5m_input_tokens": 0})])
    found = compaction.read_context(path)
    assert found.tokens == 150_000 and not found.ttl_known and found.ttl_s == compaction.CACHE_TTL_S


def test_git_that_cannot_run_and_no_checkout_have_no_merges_and_no_unmerged_branch(tmp_path: Path, monkeypatch):
    assert compaction._unmerged(None, "cauce/task-1") is False
    assert compaction._unmerged(str(tmp_path / "gone"), "cauce/task-1") is False

    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert compaction._git(str(tmp_path), "log") is None
    assert compaction._unmerged(str(tmp_path), "cauce/task-1") is False


def test_a_context_worth_a_record_with_no_break_keeps_no_instructions(store: Store, tmp_path: Path, git_repo: Path):
    task = turn(store, git_repo)
    path = big(tmp_path, datetime.now(UTC))
    data = compaction.consider(store, home(), session_id="s1", transcript=path, cwd=str(git_repo),
                               task_id=task["id"], env={})
    assert data is not None and not data["now"] and "instructions" not in data
    assert store.events_of(task["id"], compaction.DECIDED)[0]["id"] == data["id"]


def test_a_version_s_verdict_is_told_once(tmp_path: Path):
    said = compaction._learn(tmp_path, "2.9.0", compaction.IGNORED, 1)
    assert said is not None and "(2.9.0) did not run the /compact" in said
    assert compaction._learn(tmp_path, "2.9.0", compaction.IGNORED, 2) is None
    assert compaction.builds(tmp_path)["2.9.0"]["verdict"] == compaction.IGNORED


# --- compact -------------------------------------------------------------------------------------


def test_a_line_too_long_or_that_is_no_object_is_skipped(tmp_path: Path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "x" * 200}}) + "\n[1, 2]\n"
                    + json.dumps({"type": "user", "message": {"content": "keep prices in cents"}}) + "\n")
    monkeypatch.setattr(compact, "MAX_LINE", 100)
    assert list(compact._entries(path)) == [{"type": "user", "message": {"content": "keep prices in cents"}}]


def test_a_block_that_is_no_object_is_read_past(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": [
        "a bare string", {"type": "tool_use", "name": "Write", "input": {"file_path": "/r/a.py"}}]}}) + "\n")
    assert list(compact.read(path)["written"]) == ["/r/a.py"]


def test_git_status_that_cannot_run_changed_nothing(tmp_path: Path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert compact._changed(tmp_path) == []


def test_a_section_cut_oldest_first_counts_the_rest():
    text = compact._section("Files git shows changed now:", [f"- f{n}.py" for n in range(10)], 60)
    assert text.startswith("Files git shows changed now:\n- f0.py\n")
    assert text.splitlines()[-1].startswith("- (+") and text.endswith(" more)")


def test_a_session_with_nothing_to_keep_writes_no_digest(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
                    + "\n")
    assert compact.keep(tmp_path / "home", "s1", path, None, []) is None
    assert not (tmp_path / "home").exists()
