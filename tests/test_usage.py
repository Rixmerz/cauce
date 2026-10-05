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
