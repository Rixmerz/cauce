from __future__ import annotations

import http.client
import json
import threading

import pytest

from cauce.store import Store
from cauce.ui import api, server

# --- the data each screen shows -------------------------------------------------------

def test_the_board_sorts_work_by_what_it_needs(store: Store):
    failed = store.create_task("broke", status="failed", source="cauce", repo="r", kind="implement")
    running = store.create_task("going", status="running", source="cauce", repo="r", current_cell="sonnet/high")
    store.add_event(running["id"], "attempt_started", seq=2, cell="sonnet/high")
    done = store.create_task("shipped", status="done", source="cauce", repo="r")
    store.add_event(done["id"], "finished", status="done", branch="cauce/task-3")
    store.create_task("asked in a session", status="failed", source="hook", session_id="s")
    store.create_task("child", status="running", source="delegation", session_id="s")
    store.enqueue("later", repo="r", cwd="/x")
    store.pause_lane("other", "task #9 ended failed")
    b = api.board(store)
    assert [t["id"] for t in b["needs_you"]] == [done["id"], failed["id"]]
    assert "review branch cauce/task-3" in b["needs_you"][0]["asks"]
    assert b["needs_you"][1]["asks"].startswith("every cell")
    assert b["running"][0]["attempt"] == 2 and b["running"][0]["current_cell"] == "sonnet/high"
    assert {lane["repo"]: lane["paused"] for lane in b["queued"]} == {"other": True, "r": False}
    assert b["counts"] == {"needs_you": 2, "running": 1, "workers": 0, "queued": 1, "done": 1}
    assert b["last_event"] == store.last_event_id()


def test_the_board_shows_each_task_s_way_and_the_worker_out_now(store: Store):
    """The flow: kind, ladder, start and every attempt with its move. The worker: the claude -p cauce
    launched for the attempt in flight, gone from the board once that attempt is recorded."""
    import os

    t = store.create_task("going", status="running", source="cauce", repo="r", pid=os.getpid())
    store.add_event(t["id"], "planned", kind="implement", start="sonnet/low",
                    ladder=["sonnet/low", "sonnet/medium", "opus/medium"], reasons=["history raised it"])
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=0, failure="code_bug", cost_usd=0.1)
    store.set_move(t["id"], 1, "more_effort", "the work was shallow")
    store.add_event(t["id"], "attempt_started", seq=2, cell="sonnet/medium", max_turns=30, budget_usd=1.9,
                    capabilities=["livespec"])
    store.create_task("in a session", status="running", source="hook", session_id="s")
    b = api.board(store)
    going = next(r for r in b["running"] if r["id"] == t["id"])
    assert going["flow"]["ladder"] == ["sonnet/low", "sonnet/medium", "opus/medium"]
    assert going["flow"]["steps"][0]["move"] == "more_effort" and going["flow"]["steps"][0]["failure"] == "code_bug"
    assert going["worker"]["seq"] == 2 and going["worker"]["cell"] == "sonnet/medium"
    assert going["worker"]["capabilities"] == ["livespec"] and going["worker"]["alive"] is True
    assert next(r for r in b["running"] if r["source"] == "hook").get("flow") is None
    assert b["counts"]["workers"] == 1

    store.add_attempt(t["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="approach")
    assert next(r for r in api.board(store)["running"] if r["id"] == t["id"])["worker"] is None  # between attempts
    assert api.flow_of(store, 999) is None


def test_task_detail_spend_routing_memory_habits(store: Store):
    t = store.create_task("fix it", status="done", source="cauce", repo="r", kind="docs", start_cell="haiku",
                          final_cell="sonnet/low", cost_usd=0.3)
    store.add_attempt(t["id"], cell="haiku", max_turns=30, passed=0, failure="code_bug", changed_paths=["a.py"],
                      capabilities=["livespec"], cost_usd=0.1)
    store.add_attempt(t["id"], cell="sonnet/low", max_turns=30, passed=1, cost_usd=0.2)
    store.add_event(t["id"], "planned", ladder=["haiku"], start="haiku", reasons=[])
    store.add_event(t["id"], "finished", status="done", branch="b", impact=["livespec: x"])
    d = api.task_detail(store, t["id"])
    assert d["attempts"][0]["changed_paths"] == ["a.py"] and d["branch"] == "b" and d["impact"] == ["livespec: x"]
    assert d["plan"]["start"] == "haiku" and api.task_detail(store, 999) is None

    s = api.spend(store, days=7)
    assert s["workers"][0]["cell"] in ("haiku", "sonnet/low") and s["by_day"][0]["workers"] == pytest.approx(0.3)

    docs = next(r for r in api.routing(store) if r["kind"] == "docs")
    assert docs["tasks"] == 1 and docs["climbed"] == 1 and docs["passes"] == {"sonnet/low": 1}
    assert next(r for r in api.routing(store) if r["kind"] == "plan")["tasks"] == 0

    p = store.open_problem("slow import", repo="r")
    store.add_fix(p, "lazy load", "failed", repo="r")
    assert api.memory(store)[0]["title"] == "slow import"
    assert api.memory(store, "slow import")[0]["id"] == p

    for s_ in range(3):
        for sig in ("edit:.py", "bash:pytest"):
            store.add_tool_event(session_id=f"s{s_}", tool="x", sig=sig, arg_hash="h", ok=1)
    view = api.habit_view(store)
    assert view["candidates"][0]["steps"] == ["edit:.py", "bash:pytest"] and view["installed"] == []


# --- the server and its envelope --------------------------------------------------------

@pytest.fixture
def ui(_isolated_home):
    srv = server.serve(0, _isolated_home)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.stop_event.set()
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def token(srv):
    _, data = call(srv, "GET", "/api/token", headers={"Sec-Fetch-Site": "same-origin"})
    return json.loads(data)["token"]


def test_pages_and_reads(ui):
    res, data = call(ui, "GET", "/")
    assert res.status == 200 and b"/static/main.js" in data
    assert "script-src 'self'" in res.getheader("Content-Security-Policy")
    assert res.getheader("X-Content-Type-Options") == "nosniff"
    res, data = call(ui, "GET", "/static/board.js")
    assert res.status == 200 and res.getheader("Content-Type").startswith("text/javascript")
    for path in ("/api/board", "/api/spend?days=3", "/api/routing", "/api/memory?q=x", "/api/habits",
                 "/api/events?after=0"):
        res, data = call(ui, "GET", path)
        assert res.status == 200, path
        json.loads(data)
    assert call(ui, "GET", "/api/tasks/99")[0].status == 404
    assert call(ui, "GET", "/api/nope")[0].status == 404
    assert call(ui, "HEAD", "/")[0].status == 200


@pytest.mark.parametrize("path", ["/static/../store.py", "/static/x.py", "/static/.hidden.js", "/static/missing.js"])
def test_static_files_are_a_closed_list(ui, path):
    assert call(ui, "GET", path)[0].status == 404


def test_a_foreign_host_is_refused(ui):
    # A DNS-rebinding page reaches 127.0.0.1 under its own name.
    assert call(ui, "GET", "/api/board", host="evil.example:80")[0].status == 421
    assert call(ui, "GET", "/", host="localhost:1")[0].status == 421
    port = ui.server_address[1]
    assert call(ui, "GET", "/api/board", host=f"localhost:{port}")[0].status == 200


def test_the_token_is_only_for_the_page_and_every_write_needs_it(ui, git_repo):
    assert call(ui, "GET", "/api/token")[0].status == 403
    assert call(ui, "GET", "/api/token", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    key = token(ui)
    assert len(key) == 64
    assert call(ui, "OPTIONS", "/api/queue")[0].status == 403
    body = {"text": "write docs", "repo_dir": str(git_repo)}
    assert call(ui, "POST", "/api/queue", body)[0].status == 403
    assert call(ui, "POST", "/api/queue", body, headers={"X-Cauce-Token": "wrong"})[0].status == 403
    res, data = call(ui, "POST", "/api/queue", body, headers={"X-Cauce-Token": key})
    assert res.status == 201 and json.loads(data)["title"] == "write docs"
    form = call(ui, "POST", "/api/queue", b"text=x", headers={"X-Cauce-Token": key,
                                                               "Content-Type": "application/x-www-form-urlencoded"})
    assert form[0].status == 415
    assert call(ui, "POST", "/api/queue", b"{bad", headers={"X-Cauce-Token": key})[0].status == 400
    assert call(ui, "POST", "/api/queue", b"[1]", headers={"X-Cauce-Token": key})[0].status == 400
    assert call(ui, "POST", "/api/queue", {"text": "x", "repo_dir": "/no/such/dir"},
                headers={"X-Cauce-Token": key})[0].status == 400
    # Refused on the declared length, before a byte of it is read. Sending the
    # whole body would race the refusal: the server closes while the client writes.
    big = {"X-Cauce-Token": key, "Content-Length": str(server.MAX_BODY + 1)}
    assert call(ui, "POST", "/api/queue", b"{}", headers=big)[0].status == 413

    # deleting the token file revokes every open tab
    server.token_path(ui.root).unlink()
    assert call(ui, "POST", "/api/queue", body, headers={"X-Cauce-Token": key})[0].status == 403


def test_cancel_unpause_and_work(ui, git_repo, monkeypatch):
    key = {"X-Cauce-Token": token(ui)}
    store = Store(ui.root / "cauce.db")
    queued = store.enqueue("x", repo="r", cwd=str(git_repo))
    running = store.create_task("y", status="running", source="cauce", pid=None)
    store.pause_lane("r", "failed")
    assert call(ui, "POST", f"/api/tasks/{queued['id']}/cancel", {}, headers=key)[0].status == 200
    assert call(ui, "POST", f"/api/tasks/{running['id']}/cancel", {}, headers=key)[0].status == 200
    assert call(ui, "POST", "/api/tasks/999/cancel", {}, headers=key)[0].status == 404
    assert store.get_task(queued["id"])["status"] == "cancelled" and store.cancel_requested(running["id"])
    assert call(ui, "POST", "/api/lanes/unpause", {"repo": "r"}, headers=key)[0].status == 200
    assert not store.lanes()[0]["paused"]
    started = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert call(ui, "POST", "/api/work", {"repo_dir": str(git_repo)}, headers=key)[0].status == 202
    assert call(ui, "POST", "/api/work", {}, headers=key)[0].status == 202
    assert started[0][-3:] == ["work", "--repo", str(git_repo)] and started[1][-2:] == ["work", "--all"]
    assert call(ui, "POST", "/api/nope", {}, headers=key)[0].status == 404
    store.close()


def test_the_stream_sends_events_and_is_bounded(ui, monkeypatch):
    store = Store(ui.root / "cauce.db")
    t = store.create_task("x", status="running", source="cauce")
    store.add_event(t["id"], "planned", kind="docs")
    store.close()
    monkeypatch.setattr(server, "STREAM_SECONDS", 1)
    res, data = call(ui, "GET", "/api/stream?after=0")
    assert res.getheader("Content-Type") == "text/event-stream"
    assert b'"kind": "planned"' in data and b"id: 1" in data
    for _ in range(server.MAX_STREAMS):
        ui.streams.acquire()
    try:
        assert call(ui, "GET", "/api/stream")[0].status == 503
    finally:
        for _ in range(server.MAX_STREAMS):
            ui.streams.release()


def test_a_crash_is_a_bare_500_and_a_logged_trace(ui, monkeypatch):
    def boom(store):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(api, "board", boom)
    res, data = call(ui, "GET", "/api/board")
    assert res.status == 500 and b"secret detail" not in data
    assert "secret detail" in (ui.root / "ui-errors.log").read_text()


def test_housekeeping_sweeps_without_a_page(_isolated_home, monkeypatch):
    srv = server.UIServer(0, _isolated_home)
    store = Store(_isolated_home / "cauce.db")
    dead = store.create_task("dead", status="running", source="cauce", pid=999999)
    monkeypatch.setattr(server, "SWEEP_EVERY_S", 0.05)
    thread = threading.Thread(target=srv.housekeeping, daemon=True)
    thread.start()
    import time
    deadline = time.monotonic() + 5
    while store.get_task(dead["id"])["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    srv.stop_event.set()
    thread.join(timeout=5)
    srv.server_close()
    assert store.get_task(dead["id"])["status"] == "interrupted"
    store.close()


def test_the_int_parser():
    assert server._int({"days": ["x"]}, "days", 7, 1, 30) == 7
    assert server._int({"days": ["999"]}, "days", 7, 1, 30) == 30
    assert server._int({}, "days", 7, 1, 30) == 7
