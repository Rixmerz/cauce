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
           "CAUCE_AUTOWORK": "on", "CLAUDE_CODE_SESSION_ID": "host-session",
           "CLAUDE_CODE_REMOTE_SESSION_ID": "host-session",
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
