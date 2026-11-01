"""Branches of `locks.scribe` and `locks.runtime` their suites leave unread."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from locks import runtime, scribe, usage

# --- scribe --------------------------------------------------------------------------------------


def _transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_a_session_id_that_is_no_file_name_is_neither_loaded_nor_saved(tmp_path: Path):
    assert scribe.load(tmp_path, "../escape") is None
    assert scribe.save(tmp_path, {"session": "../escape"}) is None
    assert not (tmp_path / "scribe").exists()


def test_a_transcript_that_fails_mid_read_keeps_the_state_unsaved(tmp_path: Path, monkeypatch):
    transcript = _transcript(tmp_path / "s1.jsonl", [{"type": "user", "message": {"content": "hello there"}}])

    def broken(path, offset):
        raise OSError("the disk went away")
        yield  # pragma: no cover - a generator that raises at its first step

    monkeypatch.setattr(usage, "_records", broken)
    state = scribe.update(tmp_path, "s1", transcript)
    assert state["offset"] == 0 and state["requests"] == []
    assert scribe.load(tmp_path, "s1") is None


def test_the_scribe_reads_what_has_no_stamp_odd_blocks_and_a_failed_read(tmp_path: Path):
    transcript = _transcript(tmp_path / "s2.jsonl", [
        {"type": "user", "message": {"content": "fix the cart totals please"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "message": {"content": [
            "a bare string", {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/x/cart.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "r1", "is_error": True, "content": "EACCES: permission denied"}]}},
    ])
    state = scribe.update(tmp_path, "s2", transcript)
    assert state["started"] == "2026-10-01T10:00:00Z"
    assert [r["text"] for r in state["requests"]] == ["fix the cart totals please"]
    assert state["failures"] and state["failures"][0]["call"].startswith("Read `/x/cart.py`")
    assert state["open"] == {}  # a failed read is no shell command left failing


def test_a_budget_too_small_for_any_section_carries_nothing():
    state = {"requests": [{"text": "fix the cart totals in checkout", "correction": False}],
             "written": ["/repo/src/cart.py"], "open": {"pytest": {"tries": 2, "error": "1 failed"}}}
    parts, got = scribe.context_for(state, "fix the cart totals", budget=90)
    assert "".join(parts) == "" and got == {"requests": 0, "files": 0, "failing": 0, "chars": 0}


def test_commits_since_is_nothing_when_git_cannot_run(tmp_path: Path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert scribe.commits_since(str(tmp_path), "2026-10-01T00:00:00Z") == []


# --- runtime -------------------------------------------------------------------------------------


def test_an_empty_version_file_and_a_broken_manifest_are_read_past(tmp_path: Path):
    (tmp_path / ".nvmrc").write_text("\n")
    (tmp_path / ".node-version").write_text("22.4.0\n")
    assert runtime.wanted(tmp_path) == ("22.4.0", ".node-version")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "package.json").write_text("{ not json")
    assert runtime.wanted(other) is None


def test_only_nvm_folders_named_as_a_version_with_a_node_count(tmp_path: Path):
    root = tmp_path / "nvm" / "versions" / "node"
    for name, with_node in (("v20.11.1", True), ("v22.1.0", False), ("system", True), ("v18", True)):
        (root / name / "bin").mkdir(parents=True)
        if with_node:
            (root / name / "bin" / "node").write_text("")
    assert runtime.installed({"NVM_DIR": str(tmp_path / "nvm")}) == [((20, 11, 1), root / "v20.11.1" / "bin")]


def test_a_node_that_cannot_answer_has_no_version(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").write_text("#!/bin/sh\necho v22.4.0\n")
    (bin_dir / "node").chmod(0o755)
    env = {"PATH": str(bin_dir)}
    assert runtime.on_path(env) == (22, 4, 0)

    def broken(*a, **kw):
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", broken)
    assert runtime.on_path(env) is None
    assert os.environ.get("PATH")


def test_greater_and_at_most_compare_by_the_whole_version():
    assert runtime.satisfies((22, 1, 0), ">22")
    assert not runtime.satisfies((22, 0, 0), ">22")
    assert runtime.satisfies((18, 0, 0), "<=18")
    assert not runtime.satisfies((18, 0, 1), "<=18")
    assert runtime._version("22.1") is None
    assert runtime._version("22.1", partial=True) == (22, 1)
