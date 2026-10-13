from __future__ import annotations

import json

from cauce import usage
from cauce.store import Store


def line(msg_id, model="claude-opus-5-5", out=10, sidechain=False, **usage_extra):
    return json.dumps({"type": "assistant", "sessionId": "s", "isSidechain": sidechain,
                       "timestamp": "2099-01-01T00:00:00Z",
                       "message": {"id": msg_id, "model": model,
                                   "usage": {"input_tokens": 2, "output_tokens": out,
                                             "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100,
                                             **usage_extra}}}) + "\n"


def test_one_record_per_message_and_reads_resume_where_they_stopped(store: Store, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(line("m1", out=5) + line("m1", out=7) + "not json\n" + json.dumps({"type": "user"}) + "\n"
                    + line("m2", model="claude-haiku-4-5"))
    assert usage.ingest(store, path, session_id="s", task_id=None, repo="r") == 2
    rows = {r["model"]: r for r in store.usage_by_model(days=100000, repo="r")}
    assert rows["claude-opus-5-5"]["output_tokens"] == 7  # the last line of a streamed message wins
    assert usage.ingest(store, path, session_id="s", task_id=None, repo="r") == 0  # nothing new
    with path.open("a") as fh:
        fh.write(line("m3") + line("m4")[:20])  # m4 is still being written
    assert usage.ingest(store, path, session_id="s", task_id=None, repo="r") == 1
    with path.open("a") as fh:
        fh.write(line("m4")[20:])
    assert usage.ingest(store, path, session_id="s", task_id=None, repo="r") == 1
    path.write_text(line("m5"))  # replaced: shorter than the offset
    assert usage.ingest(store, path, session_id="s", task_id=None, repo="r") == 1
    assert usage.ingest(store, tmp_path / "missing.jsonl", session_id="s", task_id=None, repo=None) == 0


def test_a_runaway_line_is_skipped_not_fatal(store: Store, tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "MAX_LINE", 1000)
    path = tmp_path / "t.jsonl"
    path.write_text("y" * 5000 + "\n" + line("m1") + "z" * 3000 + "\n" + line("m2"))
    assert usage.ingest(store, path, session_id="s", task_id=None, repo=None) == 2
    assert store.transcript_offset(str(path)) == path.stat().st_size


def test_dollars_and_family():
    assert usage.family("claude-sonnet-5-5") == "sonnet" and usage.family("gpt") == "other"
    assert usage.family(None) == "other"
    assert usage.dollars("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert usage.dollars("claude-opus-5-5", 0, 0, cache_read=1_000_000) == 0.4
    assert usage.dollars("claude-opus-5-5", 0, 0, cache_write=1_000_000) == 5.0
    assert usage.dollars("mystery", 1, 1) == 0.0


def test_spend_joins_sessions_and_workers(store: Store, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(line("m1", out=1_000_000))
    usage.ingest(store, path, session_id="s", task_id=None, repo="r")
    task = store.create_task("x", status="done", source="cauce", repo="r", kind="docs")
    store.add_attempt(task["id"], cell="haiku", max_turns=30, passed=1, cost_usd=0.25)
    report = usage.spend(store, days=100000, repo="r")
    assert report["sessions"][0]["family"] == "opus" and report["sessions"][0]["usd"] > 20
    assert report["workers"][0]["cell"] == "haiku" and report["workers"][0]["usd"] == 0.25
    assert report["total_usd"] == round(report["sessions"][0]["usd"] + 0.25, 4)


def test_subagents_are_counted_to_the_delegation_that_started_them(store: Store, tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(line("m1"))
    folder = usage.subagents_folder(transcript)
    assert usage.ingest_subagents(store, transcript, session_id="s", task_id=None, repo="r") == 0  # none yet
    folder.mkdir(parents=True)
    parent = store.create_task("build it", status="running", source="hook", session_id="s", repo="r")
    child = store.create_task("write the server", status="running", source="delegation", session_id="s",
                              parent_id=parent["id"], repo="r", prompt_id="tu1")
    (folder / "agent-a1.jsonl").write_text(line("m2", sidechain=True, out=3000))
    (folder / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "tu1", "agentType": "general-purpose"}))
    (folder / "agent-a2.jsonl").write_text(line("m3", sidechain=True))  # no meta: the turn's task
    (folder / "agent-a3.jsonl").write_text(line("m4", sidechain=True))
    (folder / "agent-a3.meta.json").write_text("not json")
    assert usage.subagent_tool_use(transcript, "a1") == "tu1" and usage.subagent_tool_use(transcript, "a3") is None
    assert usage.ingest_subagents(store, transcript, session_id="s", task_id=parent["id"], repo="r") == 3
    assert usage.ingest_subagents(store, transcript, session_id="s", task_id=parent["id"], repo="r") == 0
    by_task = usage.cost_by(store, "task_id", [parent["id"], child["id"]])
    assert set(by_task) == {parent["id"], child["id"]} and by_task[child["id"]] > by_task[parent["id"]]
    assert usage.cost_by(store, "session_id", ["s"])["s"] == round(sum(by_task.values()), 4)
    assert usage.cost_by(store, "task_id", []) == {}
    assert store.usage_by_model(days=100000, repo="r")[0]["messages"] == 3


def test_usage_is_kept_by_task_or_session_only(store: Store):
    import pytest

    with pytest.raises(ValueError, match="task_id or session_id"):
        store.usage_by("repo", ["r"])
