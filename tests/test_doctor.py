"""cauce doctor: every rule is a bug cauce shipped, turned into a query over the store."""
import json
from datetime import UTC, datetime, timedelta

from cauce import cli, doctor
from cauce.store import Store

LATER = datetime.now(UTC) + timedelta(hours=1)


def rules(store: Store, **kw) -> dict[str, list[doctor.Finding]]:
    out: dict[str, list[doctor.Finding]] = {}
    for found in doctor.check(store, days=0, **kw):
        out.setdefault(found.rule, []).append(found)
    return out


def test_a_clean_store_has_nothing_to_say(store: Store):
    assert doctor.check(store) == []
    assert doctor.text([]) == "cauce doctor: nothing broken, nothing suspect."


def test_a_lane_paused_on_a_task_that_passed_is_broken_and_one_paused_on_a_live_stop_is_not(store: Store):
    passed = store.create_task("x", status="done", source="cauce", repo="r")
    store.pause_lane("r", f"task #{passed['id']}: its process exited unexpectedly")
    store.enqueue("next", repo="r", cwd="/")
    found = rules(store, now=LATER, alive=lambda pid: True)["lane-paused-on-ended"][0]
    assert found.severity == doctor.BROKEN and found.task_id == passed["id"]
    assert "1 queued task(s) wait on nothing" in found.what and found.fix == "cauce lanes --unpause r"
    stopped = store.create_task("y", status="blocked", source="cauce", repo="q")
    store.pause_lane("q", f"task #{stopped['id']} ended blocked: a rule")
    assert [f.repo for f in rules(store, now=LATER, alive=lambda pid: True)["lane-paused-on-ended"]] == ["r"]


def test_running_with_its_process_gone_and_queued_work_nobody_started_are_broken(store: Store):
    dead = store.create_task("x", status="running", source="cauce", repo="r", pid=999999)
    store.create_task("y", status="running", source="cauce", repo="r", pid=1)
    waiting = store.enqueue("z", repo="q", cwd="/")
    found = rules(store, now=LATER, alive=lambda pid: pid == 1)
    assert [f.task_id for f in found["running-with-no-process"]] == [dead["id"]]
    # The lane with running work is busy, not stuck; the other one is.
    assert [f.task_id for f in found["queued-with-no-dispatcher"]] == [waiting["id"]]
    assert "queued-with-no-dispatcher" not in rules(store, now=datetime.now(UTC), alive=lambda pid: True)


def test_work_held_behind_work_that_left_the_board_is_broken(store: Store):
    gone = store.create_task("x", status="dismissed", source="cauce", repo="r")
    live = store.create_task("y", status="blocked", source="cauce", repo="r")
    stuck = store.enqueue("after x", repo="r", cwd="/", options={"after": {"task": gone["id"]}})
    store.enqueue("after y", repo="r", cwd="/", options={"after": {"task": live["id"]}})
    lost = store.enqueue("after a run", repo="r", cwd="/", options={"after": {"run": 99}})
    found = rules(store, now=LATER, alive=lambda pid: True)["held-behind-gone"]
    assert sorted(f.task_id for f in found) == [stuck["id"], lost["id"]]
    assert all(f.severity == doctor.BROKEN and f.fix.startswith(f"cauce dismiss {f.task_id}") for f in found)
    assert "left the board" in next(f for f in found if f.task_id == stuck["id"]).what


def test_an_automatic_resume_that_started_no_attempt_is_broken(store: Store):
    task = store.create_task("x", status="interrupted", source="cauce", repo="r")
    store.add_event(task["id"], "auto_resumed", cause="died", after="killed")
    assert rules(store, now=LATER, alive=lambda pid: True)["resume-never-ran"][0].task_id == task["id"]
    store.add_event(task["id"], "attempt_started", seq=2, cell="sonnet/high", max_turns=80)
    assert "resume-never-ran" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_part_that_lost_its_mark_and_a_join_that_skipped_kept_branches_are_suspect(store: Store):
    part = {"id": "api", "n": 1, "of": 2, "plan": 1}
    a = store.create_task("a", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                          options=json.dumps({"part": part}))
    b = store.create_task("b", status="done", source="queue", repo="r", workflow_run=7, workflow_step="build",
                          options=json.dumps({"kind": "feature"}))
    for t in (a, b):
        store.add_event(t["id"], "finished", status="done", branch=f"cauce/task-{t['id']}")
    join = store.create_task("join", status="done", source="queue", repo="r", workflow_run=7,
                             workflow_step="integrate")
    store.add_event(join["id"], "finished", status="done", branch=None, skipped=True)
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert [f.task_id for f in found["part-lost-its-mark"]] == [b["id"]]
    # Only one part still says it is one, so the join reads one branch: the rule sees the lost one too
    # once its mark is back, as the store held it before the fix.
    store.update_task(b["id"], options=json.dumps({"part": {**part, "id": "web", "n": 2}}))
    assert rules(store, now=LATER, alive=lambda pid: True)["join-short-of-parts"][0].task_id == join["id"]


def test_worktree_attempts_that_keep_no_session_and_low_turns_and_format_climbs_are_suspect(store: Store):
    for n in range(3):
        t = store.create_task(f"t{n}", status="done", source="cauce", repo="r", kind="implement")
        store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40)
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1, session_id="s" if n == 0 else None)
    asked = store.create_task("asked", status="done", source="cauce", repo="r", kind="implement",
                              options=json.dumps({"max_turns": 12}))
    store.add_event(asked["id"], "attempt_started", seq=1, base="abc", max_turns=12)
    plan = store.create_task("plan", status="done", source="cauce", repo="r", kind="plan")
    store.add_attempt(plan["id"], cell="opus/xhigh", max_turns=80, passed=0, failure="inconclusive",
                      summary="claimed a pass, but its plan cannot run in parts: part 'x' has no prompt")
    store.add_event(plan["id"], "moved", seq=1, axis="effort", trigger="inconclusive", from_cell="opus/xhigh",
                    to_cell="opus/max")
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert "2 of 3 worktree attempts kept no session id" in found["attempts-with-no-session"][0].what
    assert found["turns-under-the-kind"][0].what == "3 implement task(s) started under the kind's 80 turns (latest #3)"
    assert found["format-failure-climbed"][0].task_id == plan["id"]
    assert "opus/xhigh → opus/max" in doctor.text(doctor.check(store, days=0))


def test_the_command_exits_1_only_when_something_is_broken(store: Store, capsys):
    assert cli.main(["doctor"]) == 0
    passed = store.create_task("x", status="done", source="cauce", repo="r")
    store.pause_lane("r", f"task #{passed['id']} ended done")
    assert cli.main(["doctor", "--json"]) == 1
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])[0]["rule"] == "lane-paused-on-ended"


def test_records_from_before_the_release_that_fixes_a_rule_are_history_never_a_sign(store: Store):
    for n, version in enumerate(("0.29.1", "0.29.1", "0.29.1", None)):
        t = store.create_task(f"t{n}", status="done", source="cauce", repo="r", kind="implement")
        store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40, version=version)
        store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1)
    found = rules(store, now=LATER, alive=lambda pid: True)
    assert {f.severity for fs in found.values() for f in fs} == {doctor.FIXED}
    assert found["attempts-with-no-session"][0].what == (
        "4 record(s) from before 0.29.2, the release that fixes it (latest #4)")
    said = doctor.text(doctor.check(store, days=0))
    assert said.startswith("cauce doctor: nothing broken, nothing suspect.\nfixed since, history only:")
    # The same bug written by a release that fixes it is the bug come back.
    t = store.create_task("t", status="done", source="cauce", repo="r", kind="implement")
    store.add_event(t["id"], "attempt_started", seq=1, base="abc", max_turns=40, version="0.29.2")
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=40, passed=1)
    back = rules(store, now=LATER, alive=lambda pid: True)["turns-under-the-kind"]
    assert [(f.severity, f.task_id) for f in back] == [(doctor.SUSPECT, t["id"]), (doctor.FIXED, 4)]


def test_the_events_doctor_reads_keep_the_version_that_wrote_them(store: Store):
    from cauce import __version__

    t = store.create_task("t", status="done", source="cauce", repo="r")
    for kind in ("planned", "attempt_started", "moved", "finished", "report"):
        store.add_event(t["id"], kind, seq=1)
    kept = {e["kind"]: e["data"].get("version") for e in store.events(task_id=t["id"])}
    assert kept == {"planned": __version__, "attempt_started": __version__, "moved": __version__,
                    "finished": __version__, "report": None}


def test_a_fix_round_that_did_not_start_on_the_round_before_is_suspect(store: Store):
    run = store.create_run(name="feature", scope="bundled", source="x", repo="r",
                           definition=json.dumps({"name": "feature", "steps": []}))
    first = store.create_task("fix", status="done", source="queue", repo="r", workflow_run=run["id"],
                              workflow_step="fix", options=json.dumps({"ledger": {"role": "fix", "round": 1,
                                                                                  "base": "b0"}}))
    store.add_event(first["id"], "finished", status="done", branch=f"cauce/task-{first['id']}", tip="t1")
    for base, version in (("b0", "0.31.0"), ("t1", "0.31.0")):
        again = store.create_task("fix", status="done", source="queue", repo="r", workflow_run=run["id"],
                                  workflow_step="fix", options=json.dumps({"ledger": {"role": "fix", "round": 2,
                                                                                      "base": base}}))
        store.add_event(again["id"], "planned", version=version)
    found = rules(store, now=LATER, alive=lambda pid: True).get("fix-round-off-base", [])
    # Only the round that started where round 1 started is off its base.
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, first["id"] + 1)]


def test_a_note_made_current_as_proven_with_too_few_briefs_that_carried_it_is_suspect(store: Store):
    from cauce import notes

    writer = store.create_task("x", status="done", source="cauce", repo="r")
    note = store.add_note("r", "code", "t", "Totals are rounded half up", author="worker", filed_by="haiku",
                          task_id=writer["id"], state=notes.CANDIDATE)
    reader = store.create_task("y", status="done", source="cauce", repo="r")
    store.add_event(reader["id"], "briefed", **{notes.BRIEFED: [note]})
    store.add_event(writer["id"], "briefed", **{notes.BRIEFED: [note]})  # its own brief proves nothing
    store.update_note(note, state="current", state_reason=notes.PROVEN + "in the brief of a guess")
    found = rules(store, now=LATER, alive=lambda pid: True)["note-proven-unbriefed"]
    assert [(f.severity, f.repo) for f in found] == [(doctor.SUSPECT, "r")] and f"note #{note}" in found[0].what
    assert doctor.FIXED_IN["note-proven-unbriefed"] == "0.32.0"
    # Proven by the tasks that read it, or a note a person confirmed: nothing to say.
    more = store.create_task("z", status="done", source="cauce", repo="r")
    store.add_event(more["id"], "briefed", **{notes.BRIEFED: [note]})
    person = store.add_note("r", "code", "u", "Invoices are numbered per tenant", author="person", filed_by="person")
    store.update_note(person, state="current", state_reason=None)
    assert "note-proven-unbriefed" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_wait_on_a_decision_about_work_that_all_ended_is_broken(store: Store):
    gone = store.create_task("rewrite the importer", status="done", source="queue", repo="r")
    long = store.create_task("build the long feature", status="running", source="queue", repo="r", pid=1)
    held = store.create_task("add a flag", status="queued", source="queue", repo="r")
    store.set_parallel(held["id"], False, "it changes the importer")
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="x", ahead=[gone["id"]])
    found = rules(store, now=LATER, alive=lambda pid: True)["wait-judged-on-gone"]
    assert [f.task_id for f in found] == [held["id"]] and found[0].fix.startswith(f"cauce lanes --beside {held['id']}")
    store.add_event(held["id"], "dispatch_decided", parallel=False, reason="x", ahead=[long["id"]])
    assert "wait-judged-on-gone" not in rules(store, now=LATER, alive=lambda pid: True)


def test_a_problem_left_open_after_its_task_passed_is_suspect(store: Store):
    task = store.create_task("fix the ble race", status="done", source="cauce", repo="r")
    problem = store.open_problem("fix the ble race", repo="r")
    store.add_fix(problem, "sonnet/high: retried", "failed", repo="r", task_id=task["id"])
    store.add_event(task["id"], "finished", status="done", version="0.40.0")
    old = store.create_task("fix the scanner", status="done", source="cauce", repo="r")
    other = store.open_problem("fix the scanner", repo="r")
    store.add_fix(other, "x", "failed", repo="r", task_id=old["id"])
    store.add_event(old["id"], "finished", status="done", version="0.39.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["problem-open-after-pass"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, task["id"]), (doctor.FIXED, old["id"])]
    store.add_fix(problem, "opus/high: serialized it", "worked", repo="r", task_id=task["id"])
    assert [f.severity for f in rules(store, now=LATER, alive=lambda pid: True)["problem-open-after-pass"]] == [
        doctor.FIXED]


def test_a_flake_fix_that_passed_on_one_run_is_suspect(store: Store):
    task = store.create_task("fix the flaky esp-now test", status="done", source="cauce", repo="r", kind="bugfix")
    store.add_event(task["id"], "finished", status="done", version="0.40.0",
                    verdict={"check": {"command": "deno task test", "passed": True}})
    held = store.create_task("fix the flaky ble test", status="done", source="cauce", repo="r", kind="bugfix")
    store.add_event(held["id"], "finished", status="done", version="0.40.0",
                    verdict={"check": {"command": "deno task test", "passed": True, "runs": 3}})
    found = rules(store, now=LATER, alive=lambda pid: True)["flake-fix-checked-once"]
    assert [(f.severity, f.task_id) for f in found] == [(doctor.SUSPECT, task["id"])]


def test_a_long_attempt_that_kept_few_turns_is_suspect(store: Store):
    task = store.create_task("ui", status="done", source="cauce", repo="r")
    store.add_event(task["id"], "attempt_finished", seq=1, wall_s=48 * 60, turns=2, version="0.40.0")
    store.add_event(task["id"], "attempt_finished", seq=2, wall_s=48 * 60, turns=40, version="0.40.0")
    store.add_event(task["id"], "attempt_finished", seq=3, wall_s=5 * 60, turns=1, version="0.40.0")
    found = rules(store, now=LATER, alive=lambda pid: True)["turns-under-the-wall"]
    assert [(f.severity, f.what) for f in found] == [(doctor.SUSPECT, f"#{task['id']} attempt 1 ran 48m and kept 2 "
                                                                      "turn(s)")]
