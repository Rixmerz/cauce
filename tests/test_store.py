from __future__ import annotations

import pytest

from cauce.store import Store, home


def test_home_honours_cauce_home_then_absolute_xdg(tmp_path):
    assert home({"CAUCE_HOME": str(tmp_path)}) == tmp_path
    assert home({"XDG_DATA_HOME": "/x"}).as_posix() == "/x/cauce"
    assert home({"XDG_DATA_HOME": "relative"}).name == "cauce"


def test_a_task_keeps_its_messages_in_order(store: Store):
    task = store.create_task("fix the parser", status="running", source="hook", session_id="s", prompt_id="p1")
    store.add_message(task["id"], "user", "also the tests")
    store.add_message(task["id"], "assistant", "done")
    assert [m["role"] for m in store.messages(task["id"])] == ["user", "user", "assistant"]
    assert store.running_task("s", "p1")["id"] == task["id"]
    assert store.running_task("s")["id"] == task["id"]
    assert store.running_task("s", "other") is None
    with pytest.raises(ValueError):
        store.create_task("x", status="bogus", source="hook")
    with pytest.raises(ValueError):
        store.update_task(task["id"], status="bogus")
    store.update_task(task["id"])  # nothing to change is not an error


def test_interrupt_sweeps_only_other_hook_tasks(store: Store):
    a = store.create_task("a", status="running", source="hook", session_id="s")
    b = store.create_task("b", status="running", source="hook", session_id="s")
    c = store.create_task("c", status="running", source="cauce", session_id="s")
    assert store.interrupt_running("s", exclude=[b["id"]]) == 1
    assert store.get_task(a["id"])["status"] == "interrupted"
    assert store.get_task(b["id"])["status"] == "running"
    assert store.get_task(c["id"])["status"] == "running"
    assert store.last_prompt_task("s")["id"] == b["id"]
    assert [t["id"] for t in store.list_tasks(session_id="s", status=["interrupted"])] == [a["id"]]


def test_attempts_add_up_and_landings_only_count_finished_core_tasks(store: Store):
    for cell in ("sonnet/high", "sonnet/xhigh", "sonnet/high"):
        t = store.create_task("t", status="running", source="cauce", kind="ui", repo="r")
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=0, cost_usd=0.5, changed_paths=["a"])
        seq = store.add_attempt(t["id"], cell=cell, max_turns=30, passed=1, cost_usd=0.25)
        store.set_move(t["id"], 1, "more_effort", "shallow")
        store.update_task(t["id"], status="done", final_cell=cell)
    assert seq == 2
    assert store.get_task(t["id"])["cost_usd"] == pytest.approx(0.75)
    assert store.attempts(t["id"])[0]["move"] == "more_effort"
    assert store.attempts(t["id"])[0]["changed_paths"] == '["a"]'
    assert store.landings("ui", repo="r") == ["sonnet/high", "sonnet/xhigh", "sonnet/high"]
    assert store.landings("ui", repo="elsewhere") == []
    hooked = store.create_task("t", status="done", source="hook", kind="ui", repo="r", final_cell="opus/max")
    assert "opus/max" not in store.landings("ui")
    assert hooked["title"] == "t"


def test_memory_finds_dead_ends_across_repositories_this_one_first(store: Store):
    other = store.open_problem("login token expires early", repo="github.com/a/other", symptom="401 after refresh")
    store.add_fix(other, "extend the token ttl", "failed", repo="github.com/a/other", why="clock skew, not ttl")
    store.add_fix(other, "sync the server clock", "worked", repo="github.com/a/other")
    here = store.open_problem("token refresh returns 401", repo="github.com/a/here")
    store.add_fix(here, "retry the refresh call", "failed", repo="github.com/a/here", why="same 401")
    assert store.open_problem("token refresh returns 401", repo="github.com/a/here") == here

    found = store.search("token 401 refresh", repo="github.com/a/here")
    assert [p["id"] for p in found] == [here, other]
    assert store.problem(other)["state"] == "solved"
    assert store.problem(999) is None

    dead = store.dead_ends("token refresh 401", repo="github.com/a/here")
    assert dead[0]["tried"] == "retry the refresh call"
    assert dead[1]["worked_instead"] == "sync the server clock"
    assert store.dead_ends(repo="github.com/a/other")[0]["tried"] == "extend the token ttl"
    assert store.dead_ends("token", limit=1) and len(store.dead_ends("token", limit=1)) == 1

    # a strict match needs several shared words; one loose word is not enough
    assert store.dead_ends("the token is fine", strict=True) == []
    assert store.dead_ends("refresh token returns 401 again", strict=True)
    assert store.search("") == [] and store.search("zzz qqq") == []
    with pytest.raises(ValueError):
        store.add_fix(here, "x", "maybe", repo=None)


def test_accents_do_not_split_words(store: Store):
    p = store.open_problem("migración de usuarios falla", repo="r")
    store.add_fix(p, "reintentar la migracion", "failed", repo="r")
    assert store.search("migracion usuarios")[0]["id"] == p


def test_two_stores_share_one_file(_isolated_home):
    a = Store.open()
    a.create_task("x", status="queued", source="cauce")
    b = Store.open()
    assert len(b.list_tasks()) == 1
    a.close()
    b.close()


def test_an_older_database_gains_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, parent_id INTEGER, "
                 "repo TEXT, cwd TEXT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', kind TEXT, "
                 "complexity TEXT, class_source TEXT, class_reason TEXT, status TEXT NOT NULL, source TEXT NOT NULL, "
                 "prompt_id TEXT, result TEXT, start_cell TEXT, final_cell TEXT, cost_usd REAL NOT NULL DEFAULT 0, "
                 "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks (title, status, source, created_at, updated_at) VALUES ('old', 'done', 'hook', "
                 "'t', 't')")
    conn.commit()
    conn.close()
    store = Store(path)
    task = store.get_task(1)
    assert task["pid"] is None and task["cancel_requested"] == 0
    assert store.request_cancel(1)["cancel_requested"] == 1 and store.cancel_requested(1)
    assert not store.cancel_requested(999)
    store.close()
    Store(path).close()  # running the migration twice changes nothing
