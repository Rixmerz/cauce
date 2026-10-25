"""Scenarios: cauce run as it runs for a person, with real processes, and only `claude` faked.

The unit tests drive the engine in-process with a Python launcher, the adaptive settings off:
they never saw a worker's process die under the dispatcher, a resume rewrite a workflow
part, a lane stay paused after its task passed, or a worker inherit the host's session.
These run the real `cauce` commands as subprocesses (the dispatcher, `run-queued`, the
automatic resume, the workflow engine, git worktrees), with `tests/scenarios/fake_claude.py`
on PATH as `claude`, and end with `cauce doctor`: whatever else a scenario asserts, the store
it leaves must hold nothing broken.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cauce import doctor
from cauce.store import Store

FAKE = Path(__file__).parent / "scenarios" / "fake_claude.py"
SRC = Path(__file__).resolve().parents[1] / "src"
#: How long a scenario may take before it is stuck: a real stuck state never ends on its own.
DEADLINE_S = 120


@pytest.fixture
def world(tmp_path: Path, git_repo: Path, _isolated_home: Path):
    """A repository, a fake `claude` first on PATH, and the environment the commands run in:
    the dispatcher starts on its own, and the host's session ids are set, as in a cloud session."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    (bin_dir / "claude").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(SRC),
           "CAUCE_AUTOWORK": "on", "CAUCE_LIMIT_GRACE_S": "1", "CAUCE_LOOP_POLL_S": "0.5",
           "CLAUDE_CODE_SESSION_ID": "host-session", "CLAUDE_CODE_REMOTE_SESSION_ID": "host-session",
           "FAKE_CLAUDE_SCRIPT": str(tmp_path / "script.json"), "FAKE_CLAUDE_LOG": str(tmp_path / "calls.jsonl")}
    env.pop("CAUCE_SESSION_ID", None)

    class World:
        repo, home, log = git_repo, _isolated_home, tmp_path / "calls.jsonl"

        def script(self, rules: list[dict]) -> None:
            Path(env["FAKE_CLAUDE_SCRIPT"]).write_text(json.dumps(rules))

        def cauce(self, *args: str) -> str:
            done = subprocess.run([sys.executable, "-m", "cauce", *args], cwd=self.repo, env=env,
                                  capture_output=True, text=True, timeout=DEADLINE_S)
            assert done.returncode == 0, done.stdout + done.stderr
            return done.stdout

        def calls(self) -> list[dict]:
            return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

        def wait(self, ended, what: str):
            """Until `ended(store)` says so; a scenario that never gets there is stuck."""
            deadline = time.monotonic() + DEADLINE_S
            while time.monotonic() < deadline:
                store = Store(self.home / "cauce.db")
                try:
                    if found := ended(store):
                        return found
                finally:
                    store.close()
                time.sleep(0.5)
            store = Store(self.home / "cauce.db")
            try:
                tasks = [(t["id"], t["status"], t["title"][:50]) for t in store.list_tasks(limit=20)]
                said = doctor.text(doctor.check(store))
            finally:
                store.close()
            logs = "\n".join(p.read_text()[-1500:] for p in (self.home / "work").glob("*.log"))
            pytest.fail(f"stuck waiting for {what}:\n{tasks}\n{said}\n{logs}")

        def healthy(self) -> None:
            store = Store(self.home / "cauce.db")
            try:
                broken = [f for f in doctor.check(store) if f.severity == doctor.BROKEN
                          or (f.severity == doctor.SUSPECT and f.rule in (
                              "part-lost-its-mark", "join-short-of-parts", "attempts-with-no-session",
                              "format-failure-climbed"))]
            finally:
                store.close()
            assert broken == [], doctor.text(broken)

    return World()


def _task(status: tuple[str, ...]):
    return lambda store: next(iter(store.list_tasks(status=list(status), limit=5)), None)


def test_a_worker_killed_mid_attempt_is_resumed_by_the_dispatcher_and_the_lane_goes_on(world):
    world.script([{"match": "make the greeting", "do": [{"die": True}, {"pass": True, "write": {"hi.txt": "hi\n"}}]}])
    world.cauce("queue", "add", "--kind", "implement", "make the greeting file for the app")
    ended = world.wait(_task(("done", "failed", "blocked", "review")), "the killed task to end")
    assert ended["status"] == "done"
    calls = world.calls()
    assert [c["action"].get("die", False) for c in calls] == [True, False]
    # A worker never sees the host's session, nor another task's identity.
    assert all(c["host"] == {} for c in calls) and all(c["worker_task"] == str(ended["id"]) for c in calls)
    world.wait(lambda store: not any(lane["paused"] for lane in store.lanes()), "the lane to open")
    world.healthy()


def test_more_turns_continue_the_worker_s_own_session(world):
    world.script([{"match": "write the long report", "do": [
        {"turns": True, "write": {"report.md": "part one\n"}},
        {"pass": True, "write": {"report.md": "part one\npart two\n"}}]}])
    world.cauce("queue", "add", "--kind", "implement", "write the long report for the release")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "replan", "interrupted")), "the report task")
    assert ended["status"] == "done"
    first, second = world.calls()
    assert "--resume" not in first["argv"]
    # The session the first attempt kept is the one the second continues, in the same worktree.
    store = Store(world.home / "cauce.db")
    try:
        kept = store.attempt(ended["id"], 1)["session_id"]
    finally:
        store.close()
    assert kept and kept != "host-session"
    assert second["argv"][second["argv"].index("--resume") + 1] == kept and second["cwd"] == first["cwd"]
    world.healthy()


def test_a_workflow_part_that_died_is_resumed_and_the_join_holds_every_part(world):
    parts = [{"id": "api", "prompt": "PART-API: write api.txt", "surfaces": ["api.txt"]},
             {"id": "web", "prompt": "PART-WEB: write web.txt", "surfaces": ["web.txt"]}]
    world.script([
        # The plan's first answer cannot run in parts: the same cell is asked again, no climb.
        {"match": "Plan, without changing any file", "do": [
            {"pass": True, "block": {"parts": [{"id": "api"}], "contract": "c"}},
            {"pass": True, "block": {"parts": parts, "contract": "api.txt and web.txt each say their name"}}]},
        {"match": "PART-API", "do": [{"pass": True, "write": {"api.txt": "api\n"}}]},
        {"match": "PART-WEB", "do": [{"die": True}, {"pass": True, "write": {"web.txt": "web\n"}}]},
        {"match": "Review, without changing any file", "do": [{"pass": True, "block": {"findings": []}}]},
    ])
    world.cauce("workflow", "run", "feature-parallel", "add the two greeting files for the app")

    def run_ended(store):
        run = store.get_run(1)
        return run if run and run["status"] not in ("active",) else None

    run = world.wait(run_ended, "the workflow run to end")
    assert run["status"] == "done", run
    store = Store(world.home / "cauce.db")
    try:
        last = max((t for t in store.run_tasks(1) if t["status"] == "done"), key=lambda t: t["id"])
        branch = next(b for t in reversed(store.run_tasks(1))
                      if (b := ((store.final_event(t["id"]) or {}).get("data") or {}).get("branch")))
    finally:
        store.close()
    assert last["workflow_step"] in ("review", "recheck")
    # The join holds both parts' work, the one that died and was resumed included.
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=world.repo,
                           capture_output=True, text=True, check=True).stdout.split()
    assert {"api.txt", "web.txt"} <= set(files), (branch, files)
    plans = [c["argv"] for c in world.calls() if c["rule"] == 0]
    assert len(plans) == 2 and plans[0][plans[0].index("--effort") + 1] == plans[1][plans[1].index("--effort") + 1]
    world.healthy()


def test_a_part_that_builds_on_another_starts_from_its_work_after_it_passes(world):
    parts = [{"id": "api", "prompt": "PART-API: write api.txt", "surfaces": ["api.txt"]},
             {"id": "web", "prompt": "PART-WEB: write web.txt from api.txt", "surfaces": ["web.txt"], "after": "api"}]
    world.script([
        {"match": "Plan, without changing any file", "do": [
            {"pass": True, "block": {"parts": parts, "contract": "web.txt repeats api.txt"}}]},
        {"match": "PART-API", "do": [{"pass": True, "write": {"api.txt": "api\n"}}]},
        {"match": "PART-WEB", "do": [{"pass": True, "write": {"web.txt": "web on api\n"}}]},
        {"match": "Review, without changing any file", "do": [{"pass": True, "block": {"findings": []}}]},
    ])
    world.cauce("workflow", "run", "feature-parallel", "add the api file and the web file built on it")

    def run_ended(store):
        run = store.get_run(1)
        return run if run and run["status"] != "active" else None

    run = world.wait(run_ended, "the workflow run to end")
    assert run["status"] == "done", run
    calls = world.calls()
    api = next(i for i, c in enumerate(calls) if c["rule"] == 1)
    web = next(i for i, c in enumerate(calls) if c["rule"] == 2)
    # The web part ran only after the api part passed, from the commit the api part kept.
    assert api < web
    store = Store(world.home / "cauce.db")
    try:
        part = {json.loads(t["options"])["part"]["id"]: t for t in store.run_tasks(1)
                if (json.loads(t["options"] or "{}").get("part") or {}).get("id")}
        released = store.last_event(part["web"]["id"], "released")
        tip = store.final_event(part["api"]["id"])["data"]["tip"]
    finally:
        store.close()
    assert released is not None and released["data"]["base"] == tip
    world.healthy()


def test_a_check_that_fails_at_the_base_the_same_way_does_not_fail_the_work(world):
    """A real run: the check's coverage thresholds of files the task never touched failed at
    the base too, and the red check was read as the work's bug. It is compared with the base."""
    (world.repo / "check.sh").write_text(
        "echo 'FAIL legacy/old.test.js'\n"
        "echo 'Jest: \"legacy/old.js\" coverage threshold for lines (80%) not met: 41.2%'\n"
        "if [ -f feature.txt ] && ! grep -q ok feature.txt; then echo 'FAIL src/feature.test.js'; fi\n"
        "exit 1\n")
    subprocess.run(["git", "add", "-A"], cwd=world.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a check that already fails"], cwd=world.repo, check=True)
    world.script([{"match": "add the feature file", "do": [{"pass": True, "write": {"feature.txt": "ok\n"}}]}])
    world.cauce("queue", "add", "--kind", "implement", "--verify", "sh check.sh", "add the feature file to the app")
    ended = world.wait(_task(("done", "failed", "blocked", "review", "replan")), "the task to end")
    assert ended["status"] == "done", ended
    store = Store(world.home / "cauce.db")
    try:
        kept = store.last_event(ended["id"], "check_preexisting")
        verdict = store.final_event(ended["id"])["data"]["verdict"]
    finally:
        store.close()
    assert kept is not None and kept["data"]["n"] == 2 and kept["data"]["added"] == 0
    assert verdict["check"]["state"] == "failing at base, unchanged"
    assert len(world.calls()) == 1  # no climb: the pass stood on its first try
    world.healthy()


def test_work_queued_after_a_task_that_is_dismissed_leaves_the_queue_with_it(world):
    world.script([{"match": "write the schema file", "do": [
        {"block": {"verdict": "fail", "failure": "spec_bug", "summary": "the schema it names does not exist"}}]}])
    world.cauce("queue", "add", "--kind", "implement", "write the schema file for the totals")
    first = world.wait(_task(("queued", "running")), "the first task to be queued")
    world.cauce("queue", "add", "--kind", "implement", "--after", str(first["id"]), "use the schema in the report")
    world.wait(lambda store: store.get_task(first["id"])["status"] not in ("queued", "running"), "the first to stop")
    said = world.cauce("dismiss", str(first["id"]), "--why", "the schema is not needed")
    assert "off the queue" in said
    store = Store(world.home / "cauce.db")
    try:
        held = next(t for t in store.list_tasks(limit=5) if t["id"] != first["id"])
        assert held["status"] == "cancelled" and json.loads(held["options"]).get("after") is None
    finally:
        store.close()
    world.healthy()


def test_a_task_the_usage_limit_stopped_resumes_on_its_own_after_the_reset(world):
    resets = int(time.time()) + 3
    world.script([{"match": "make the greeting", "do": [
        {"limit": f"Claude AI usage limit reached|{resets}", "write": {"hi.txt": "h\n"}},
        {"pass": True, "write": {"hi.txt": "hi\n"}}]}])
    world.cauce("queue", "add", "--kind", "implement", "make the greeting file for the app")
    stopped = world.wait(_task(("blocked", "done", "failed")), "the limit to stop it")
    assert stopped["status"] == "blocked"
    # A turn's end (here, the endings command) sees the limit has not reset: a waiter resumes it then.
    world.cauce("endings", "--session", "nobody")
    ended = world.wait(_task(("done",)), "the resume after the reset")
    assert ended["id"] == stopped["id"] and [c["action"].get("limit") is None for c in world.calls()] == [False, True]
    world.healthy()


#: How many tasks the long scenario runs one after another in one repository.
LONG_RUN = 30


def test_a_loop_runs_rounds_by_itself_until_its_metric_stops_moving(world, tmp_path):
    """The loop's own runner, detached, starts each round as a workflow run, measures the
    metric between rounds, and stops when it no longer moves; the session hears once."""
    folder = tmp_path / "leads"
    folder.mkdir()
    (folder / "count.txt").write_text("0\n")
    (tmp_path / "round.json").write_text(json.dumps({"name": "probe", "inputs": ["goal"], "steps": [
        {"id": "work", "prompt": "LOOP-ROUND: work toward {goal}", "kind": "explore"}]}))
    world.cauce("workflow", "save", str(tmp_path / "round.json"), "--scope", "user")
    world.script([{"match": "LOOP-ROUND", "do": [
        {"pass": True, "write": {"count.txt": "2\n"}}, {"pass": True, "write": {"count.txt": "3\n"}},
        {"pass": True, "write": {"count.txt": "3\n"}}]}])
    world.cauce("loop", "add", "leads", "--goal", "count the leads", "--workflow", "probe", "--dir",
                str(folder), "--metric", "cat count.txt", "--stale", "1", "--policy", "offer only the audit")
    world.cauce("loop", "start", "leads")
    from cauce import loops

    deadline = time.monotonic() + DEADLINE_S
    while loops.state("leads").get("status") != "stopped" and time.monotonic() < deadline:
        time.sleep(0.5)
    st = loops.state("leads")
    assert st["status"] == "stopped" and "did not improve" in st["why"], (st, loops.diary("leads"))
    rounds = loops.diary("leads")
    assert [(r["before"], r["after"]) for r in rounds] == [(0.0, 2.0), (2.0, 3.0), (3.0, 3.0)]
    briefs = [c for c in world.calls() if c["rule"] == 0]
    assert len(briefs) == 3
    shown = world.cauce("loop", "show", "leads")
    assert "metric: 0.0 → 3.0" in shown and "rounds: 3 (3 ended)" in shown
    world.healthy()


def test_thirty_tasks_in_one_repository_keep_their_reports_short_and_their_memory_true(world):
    """A day of work in one repository: what a person's session reads after each task stays
    short (a line the repository already heard today is not said again), and every problem a
    failing attempt opened is solved once its task passes."""
    from cauce import repo as repo_mod

    from .conftest import git

    workflows = world.repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  e2e:\n    runs-on: ubuntu-latest\n    steps:\n"
                                      "      - run: npx playwright test\n")
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "ci")
    fail = {"pass": True, "block": {"verdict": "fail", "failure": "code_bug", "summary": "the driver still drops it"}}
    world.script([{"match": f"radio driver {n} ", "do": [fail, {"pass": True, "write": {f"d{n}.py": "ok\n"}}]
                   if n % 2 else [{"pass": True, "write": {f"d{n}.py": "ok\n"}}]}
                  # A brief carries earlier tasks' dead ends, never later ones': the latest task's rule first.
                  for n in reversed(range(LONG_RUN))])
    reports = [world.cauce("run", "--kind", "bugfix", "--verify", "true", "--max-attempts", "3",
                           f"fix the radio driver {n} that drops the last frame of a burst")
               for n in range(LONG_RUN)]
    assert sum("CI also runs" in r for r in reports) == 1
    longest = max(len(r) for r in reports[1:])
    assert longest < 2500, max(reports[1:], key=len)
    store = Store(world.home / "cauce.db")
    try:
        assert {t["status"] for t in store.list_tasks(limit=LONG_RUN + 5)} == {"done"}
        key = repo_mod.key(world.repo)
        problems = store.recent_problems(repos=[key], limit=50)
        assert len(problems) == LONG_RUN // 2 and {p["state"] for p in problems} == {"solved"}
        assert not [f for f in doctor.check(store) if f.rule in ("problem-open-after-pass", "turns-under-the-wall")]
    finally:
        store.close()
    world.healthy()
