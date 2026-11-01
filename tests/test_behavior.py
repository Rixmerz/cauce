"""How a worker worked: an attempt's profile from its own tool calls, kept on `attempt_finished`
and read back by `locks stats`, its table and its export."""
import json
import os

from locks import analysis, behavior, signature
from locks.store import Store


def call(tool, sig, at, *, ok=1, arg="a"):
    return {"ts": f"2026-11-01T10:00:{at:02d}.000+00:00", "tool": tool, "sig": sig, "arg_hash": arg, "ok": ok}


def test_a_profile_reads_how_far_a_worker_read_before_it_wrote_and_its_fix_loops():
    calls = [call("read", "read:py", 0, arg="x"), call("grep", "grep", 2), call("read", "read:py", 4, arg="x"),
             call("edit", "edit:py", 10), call("bash", "bash:pytest", 12, ok=0), call("edit", "edit:py", 20),
             call("bash", "bash:pytest", 22), call("edit", "edit:py", 50)]
    got = behavior.profile(calls)
    assert got["calls"] == 8 and got["failed"] == 1
    assert (got["explore"], got["implement"], got["check_runs"]) == (3, 3, 2)
    assert got["calls_before_edit"] == 3 and got["s_before_edit"] == 10.0
    # A check, a write, a check again: one fix loop; the last write no check followed.
    assert got["fix_cycles"] == 1 and got["unchecked_end"] is True
    assert got["rereads"] == 1 and got["repeat_max"] == 1
    assert got["gap_max_s"] == 28.0 and got["active_s"] == 50.0
    assert got["tools"] == {"read": 2, "edit": 3, "search": 1, "shell": 2}


def test_a_worker_that_only_read_has_no_first_write_and_repeats_are_counted():
    calls = [call("bash", "bash:ls", 0), call("bash", "bash:ls", 1), call("bash", "bash:ls", 2),
             call("mcp__livespec__search", "mcp:livespec.search", 3)]
    got = behavior.profile(calls)
    assert "calls_before_edit" not in got and got["unchecked_end"] is False and got["check_runs"] == 0
    assert got["repeat_max"] == 3 and got["tools"] == {"shell": 3, "mcp:livespec": 1}
    assert behavior.profile([]) == {}
    one = behavior.profile([call("read", "read:py", 0)])
    assert "gap_max_s" not in one and one["repeat_max"] == 1


def test_the_table_adds_profiles_up_by_kind_outcome_and_cell():
    def finished(task, passed, cell, worked):
        return {"task_id": task, "kind": "attempt_finished",
                "data": {"passed": passed, "cell": cell, "behavior": worked}}

    a = behavior.profile([call("read", "read:py", 0), call("edit", "edit:py", 5), call("bash", "bash:pytest", 9)])
    b = behavior.profile([call("edit", "edit:py", 0), call("edit", "edit:py", 1, arg="b")])
    events = [finished(1, True, "sonnet/high", a), finished(2, False, "sonnet/high", b),
              finished(3, True, "opus/high", a), {"task_id": 4, "kind": "attempt_finished", "data": {"passed": True}},
              finished(9, True, "x", a)]
    got = behavior.table(events, {1: "implement", 2: "implement", 3: "implement", 4: "implement"})
    rows = {(r["kind"], r["outcome"]): r for r in got["by_kind"]}
    assert rows[("implement", "passed")]["attempts"] == 2 and rows[("implement", "failed")]["attempts"] == 1
    assert rows[("implement", "passed")]["calls_before_edit_p50"] == 1.0
    assert rows[("implement", "failed")]["never_checked_share"] == 1.0
    assert rows[("implement", "failed")]["unchecked_end_share"] == 1.0
    assert [r["cell"] for r in got["by_cell"]] == ["sonnet/high", "opus/high"]
    assert behavior.summarize([])["calls_p50"] is None


def test_an_attempt_s_profile_is_kept_and_read_back_by_stats_and_its_export(git_repo, store: Store, monkeypatch):
    """`orchestrate` keeps the worker's calls as `attempt_finished.behavior`; `locks stats` reads it
    back by kind, on the run and in the per-attempt export."""
    from locks.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    class Calling(Script):
        def __call__(self, spec):
            env = {**os.environ, **spec.env}
            for tool, command in (("Read", None), ("Edit", None), ("Bash", "pytest -q")):
                event = {"tool_name": tool, "session_id": "w",
                         "tool_input": {"command": command} if command else {"file_path": "a.py"}}
                signature.append(event, env)
            return super().__call__(spec)

    report = run("Rename the helper in notes", git_repo, store, registry={}, launcher=Calling(ok()),
                 classifier=kind("implement"))
    worked = store.last_event(report.task_id, "attempt_finished")["data"]["behavior"]
    assert worked["calls"] == 3 and worked["calls_before_edit"] == 1 and worked["check_runs"] == 1
    said = analysis.compute(store, days=0)
    assert said["behavior"]["by_kind"][0]["kind"] == "implement"
    assert "how workers worked" in analysis.text(said)
    tasks, events = store.runs_since("")
    found = analysis.runs(tasks, events)[0]
    assert found["calls"] == 3 and found["check_runs"] == 1 and found["calls_before_edit"] == 1
    rows = analysis.attempts(tasks, events)
    assert rows[0]["task"] == report.task_id and rows[0]["calls"] == 3 and rows[0]["tools"]["edit"] == 1
    csv_text = analysis.attempts_csv(rows)
    assert csv_text.splitlines()[0].startswith("task,seq,repo,kind") and "edit:1" in csv_text
    assert analysis.attempts(tasks, events, since="9999") == []


def test_the_independent_check_s_calls_are_never_its_attempt_s():
    from locks.orchestrate import _verifier_env

    assert _verifier_env({"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "2"})["LOCKS_WORKER_ATTEMPT"] == "2-verify"
    assert "LOCKS_WORKER_ATTEMPT" not in _verifier_env({"LOCKS_WORKER_TASK": "4"})
    kept = signature.entry({"tool_name": "Read"}, {"LOCKS_WORKER_TASK": "4", "LOCKS_WORKER_ATTEMPT": "2-verify"})
    assert kept["task_id"] == 4 and kept["attempt"] is None


def test_stats_attempts_prints_one_record_per_attempt(store: Store, capsys):
    from locks import cli

    task = store.create_task("x", status="done", source="locks", repo="/r", kind="implement")["id"]
    store.add_event(task, "planned", kind="implement", start="sonnet/high")
    store.add_event(task, "attempt_finished", seq=1, cell="sonnet/high", passed=True,
                    behavior={"calls": 4, "tools": {"read": 4}})
    store.add_event(task, "finished", status="done")
    assert cli.main(["stats", "--attempts", "--days", "0"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["calls"] == 4 and rows[0]["cell"] == "sonnet/high"
    assert cli.main(["stats", "--attempts", "--csv"]) == 0
    assert "read:4" in capsys.readouterr().out
