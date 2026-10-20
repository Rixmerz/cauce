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
