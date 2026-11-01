"""What a running task shows of itself: time against its kind's usual, stage, what it wrote, a quiet worker."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime

from locks import isolate, progress
from locks.cli import main
from locks.config import home

from .conftest import git
from .test_standards import finished


def _running(store, *, kind="implement", points=3, repo="r"):
    task = store.create_task("add a logout button", status="running", source="queue", repo=repo, kind=kind,
                             points=points)
    store.add_event(task["id"], "planned", kind=kind)
    store.add_event(task["id"], "attempt_started", seq=1)
    return store.get_task(task["id"])


def _at(store, task_id, kind) -> float:
    return datetime.fromisoformat(store.last_event(task_id, kind)["ts"]).timestamp()


def test_an_estimate_needs_passes_of_its_kind_and_prefers_its_size(store):
    task = _running(store)
    assert progress.estimate(store, task) is None  # no history: no guess
    for wall in (300, 400, 500):
        finished(store, ("sonnet/medium", True), points=8, wall=wall)
    found = progress.estimate(store, task)
    assert found["p50_s"] == 400 and found["by"] == "its kind"
    for wall in (60, 90, 120):
        finished(store, ("sonnet/medium", True), points=3, wall=wall)
    found = progress.estimate(store, task)
    assert found["p50_s"] == 90 and found["p90_s"] == 120 and found["by"] == "its kind and size"
    assert "≈2m here" in progress.expected_line(store, task)


def test_progress_says_the_stage_and_a_worker_gone_quiet(store):
    task = _running(store)
    began = _at(store, task["id"], "attempt_started")
    found = progress.of(store, task, now=began + 30, env={"LOCKS_HOME": str(home())})
    assert found["stage"] == "worker" and found["quiet_s"] == 30 and "no tool call" not in progress.line(found)
    quiet = progress.of(store, task, now=began + progress.IDLE_S + 60)
    assert "no tool call for 6m (a long command, or stuck)" in progress.line(quiet)
    # Once its worker is done, locks' own check runs: no worker to be quiet.
    store.add_event(task["id"], "stage", seq=1, stage="check")
    checking = progress.of(store, task, now=began + progress.IDLE_S + 60)
    assert checking["stage"] == "locks' check" and "quiet_s" not in checking
    assert progress.line(checking).startswith("locks' check")
    # A new attempt is its worker's again.
    store.add_event(task["id"], "attempt_started", seq=2)
    assert progress.of(store, task)["stage"] == "worker"


def test_what_the_worker_wrote_so_far_is_read_from_its_worktree(git_repo, store):
    task = _running(store)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    path = home() / "worktrees" / f"task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", f"locks/task-{task['id']}", str(path))
    assert progress.changes(task["id"], base) == {"files": 0, "added": 0, "removed": 0, "paths": []}
    (path / "new.py").write_text("a = 1\nb = 2\n")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_text("junk\n")  # a tool's artifact is no change
    assert progress.changes(task["id"], base) == {"files": 1, "added": 2, "removed": 0, "paths": ["new.py"]}
    assert progress.changes(999, base) is None  # no worktree: nothing to say
    # A worker that wrote through its shell made no write call: its worktree says it implements.
    from .test_checking import log

    log([(task["id"], 1, "bash:cat")])
    assert progress.of(store, task)["phase"] == "implementing"


def test_the_line_says_time_against_the_usual_and_past_it():
    p = {"stage": "worker", "phase": "implementing", "elapsed_s": 240,
         "estimate": {"p50_s": 420, "p90_s": 600, "samples": 5, "by": "its kind"},
         "changes": {"files": 3, "added": 120, "removed": 8}}
    assert progress.line(p) == "implementing · 4m of ≈7m · 3 files +120 −8"
    past = {**p, "elapsed_s": 540}
    assert progress.line(past) == "implementing · 9m, past the usual ≈7m (9 in 10 end within 10m) · 3 files +120 −8"
    late = {**p, "elapsed_s": 900, "changes": {"files": 0, "added": 0, "removed": 0}}
    assert progress.line(late) == ("implementing · 15m of ≈7m · nothing written yet · past 9 in 10 of its kind "
                                   "here (10m)")


def test_a_call_is_said_in_words_from_its_signature_alone():
    assert progress.words("bash:npm-test") == "running npm test"
    assert progress.words("bash:unknown") == "running a command"
    assert progress.words("edit:py") == "editing a .py file"
    assert progress.words("write:noext") == "writing a file"
    assert progress.words("read:ts") == "reading a .ts file"
    assert progress.words("mcp:livespec.who_calls") == "calling livespec.who_calls"
    assert progress.words("grep") == "searching the code"
    assert progress.words("") == ""


def test_progress_names_what_the_worker_is_doing_and_its_warnings(store):
    from .test_checking import log

    task = _running(store)
    assert "doing" not in progress.of(store, task)  # no call yet: nothing to say
    log([(task["id"], 1, "read:py"), (task["id"], 1, "bash:npm-test")])
    assert progress.of(store, task)["doing"] == "running npm test"
    late = {"elapsed_s": 900, "quiet_s": progress.IDLE_S + 60,
            "estimate": {"p50_s": 420, "p90_s": 600, "samples": 5, "by": "its kind"}}
    assert progress.warnings(late) == ["past 9 in 10 of its kind here (10m)",
                                       "no tool call for 6m (a long command, or stuck)"]
    assert progress.warnings({"elapsed_s": 60, "quiet_s": 30}) == []
    # `locks tasks` says the same words: its line ends with them.
    assert progress.line({"stage": "worker", **late}).endswith(" · ".join(progress.warnings(late)))


def test_a_task_look_reads_its_progress_while_it_runs_and_the_check_that_refused_it(store):
    from locks import acp

    from .test_checking import log

    task = _running(store)
    log([(task["id"], 1, "bash:npm-test")])
    assert acp.task_look(999, False) == acp.Look()  # no such task: nothing to say
    assert acp.task_look(task["id"], False).check == ""
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=30, passed=0, failure="code_bug",
                      summary="claimed a pass, but `npm test` exited 1: it works",
                      evidence="\n".join(f"line {n}" for n in range(30)))
    look = acp.task_look(task["id"], False)
    assert look.stage == "worker"
    assert look.doing == "running npm test"
    assert "checking" in look.line
    assert look.check.startswith("locks' check failed: `npm test` exited 1\nthe end of its output:\nline 10\n")
    assert look.check.endswith("line 29")
    assert len(look.check.splitlines()) - 2 == acp.CHECK_TAIL_LINES
    # An attempt the check did not turn down says nothing of a check.
    store.add_attempt(task["id"], cell="opus/high", max_turns=30, passed=1, summary="done", evidence="ok")
    assert acp.task_look(task["id"], False).check == ""


def _ended(store, status, **data):
    task = _running(store)
    store.add_event(task["id"], "finished", status=status, **data)
    store.update_task(task["id"], status=status)
    return task["id"]


def test_a_pass_ends_with_its_verdict_and_how_the_branch_is_merged(store):
    from locks import acp

    card = {"criteria": [{"id": "S1", "met": True, "evidence": "x"}, {"id": "S2", "met": False, "evidence": "y"}],
            "check": {"command": "npm test", "passed": True},
            "independent": {"ran": False, "why": "its risk (passive) does not call for one"}, "attempts": 2}
    one = _ended(store, "done", final_cell="sonnet/high", branch="locks/task-7", attempts=2, verdict=card)
    look = acp.task_look(one, True)
    assert look.ending.splitlines() == [
        f"locks: task #{one} passed at sonnet/high after 2 attempts.",
        "  check: locks ran `npm test` and it passed",
        "  criteria: 1 of 2 met (not met: S2)",
        "  independent check: not run (its risk (passive) does not call for one)",
        "The work is on branch locks/task-7; your checkout has none of it until it is merged. Next: review it "
        f"(`git diff HEAD...locks/task-7`), then `locks merge {one}` merges it, or `locks pr {one}` opens a draft "
        "pull request."]
    assert look.skipped == (acp.STAGE_ENTRIES[2],)
    assert (look.line, look.doing) == ("", "")
    bare = _ended(store, "done", verdict={"criteria": [], "check": None, "independent": {"ran": True, "passed": True}})
    ended = acp.task_look(bare, True)
    assert "  check: no check was given, so nothing ran the tests" in ended.ending
    assert ended.skipped == (acp.STAGE_ENTRIES[1],)
    in_place = _ended(store, "done", in_place=[{"repo": "api", "files": 1}])
    assert "there is no branch to merge" in acp.task_look(in_place, True).ending


def test_a_stop_ends_with_why_and_the_command_that_goes_on(store):
    from locks import acp

    stop = {"cause": "review", "by": "locks", "reason": "the independent check kept finding problems"}
    task = _ended(store, "blocked", branch="locks/task-9", stop=stop)
    ending = acp.task_look(task, True).ending
    assert ending.splitlines()[0].startswith(f"locks: task #{task} blocked. stopped by")
    assert "Its work is kept on branch locks/task-9 (`git diff HEAD...locks/task-9` shows it)." in ending
    assert f"The command that goes on: `locks resume {task}`." in ending
    assert acp.task_look(_ended(store, "failed"), True).ending.endswith("failed.")


def test_the_editor_sees_a_real_task_run_and_end_through_the_agent(store):
    """`locks acp` with nothing faked but the process: the tool call's title is the line `locks tasks` says,
    the stages move with the task's `stage` events, and the ending leads the answer."""
    from locks import acp

    from .test_acp import Client, Spawner, said, statuses
    from .test_checking import log

    task = _running(store)
    n = task["id"]
    log([(n, 1, "bash:npm-test")])
    spawner = Spawner(stderr=[f"locks: #{n} attempt 1 at sonnet/high"], hold=True)
    c = Client(spawner, look=acp.task_look, look_every=0.01)
    try:
        session = c.new_session()
        request = c.prompt(session, "add a logout button")
        live = c.out.wait_for(lambda m: "now: running npm test" in str(said(m).get("content")))
        assert said(live)["title"].startswith("checking · ")
        store.add_event(n, "stage", seq=1, stage="check")
        c.out.wait_for(lambda m: statuses(m) == ["completed", "in_progress", "pending"])
        store.add_event(n, "finished", status="done", final_cell="sonnet/high", branch=f"locks/task-{n}",
                        verdict={"criteria": [], "check": {"command": "npm test", "passed": True},
                                 "independent": {"ran": True, "passed": True}})
        store.update_task(n, status="done")
        spawner.procs[0].release()
        c.answer(request)
        text = c.updates(session)[-1]["content"]["text"]
        assert text.startswith(f"locks: task #{n} passed at sonnet/high.\n  check: locks ran `npm test`")
        assert f"`locks merge {n}`" in text
        assert [e["status"] for e in [u for u in c.updates(session) if u["sessionUpdate"] == "plan"][-1]["entries"]] \
            == ["completed"] * 3
    finally:
        spawner.procs[0].release()
        c.close()


def test_queueing_says_how_long_tasks_like_it_took(git_repo, store, capsys, monkeypatch):
    from locks import repo

    monkeypatch.setenv("LOCKS_AUTOWORK", "off")
    for wall in (100, 200, 300):
        finished(store, ("sonnet/medium", True), kind="implement", repo=repo.key(git_repo), wall=wall)
    assert main(["queue", "add", "--repo", str(git_repo), "--kind", "implement", "add a logout button"]) == 0
    assert "tasks like it took ≈3m here (9 in 10 within 5m" in capsys.readouterr().out


def test_locks_tasks_shows_a_running_task_s_progress(store, capsys):
    task = _running(store, repo=None)
    store.update_task(task["id"], source="locks")
    main(["tasks", "--all"])
    out = capsys.readouterr().out
    assert f"#{task['id']}" in out and "\n       " in out  # its progress line under it


def test_peek_shows_a_running_worker_s_diff_so_far(git_repo, store, capsys):
    from locks.ui import api

    task = _running(store)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    store.add_event(task["id"], "attempt_started", seq=1, base=base)
    path = home() / "worktrees" / f"task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", f"locks/task-{task['id']}", str(path))
    assert main(["peek", str(task["id"])]) == 0 and "nothing written yet" in capsys.readouterr().out
    (path / "logout.py").write_text("def logout():\n    return True\n")
    assert main(["peek", str(task["id"])]) == 0
    out = capsys.readouterr().out
    assert "+def logout():" in out and "logout.py" in out
    assert main(["peek", str(task["id"]), "--stat"]) == 0 and "1 file +2 −0" in capsys.readouterr().out
    found = api.task_diff(store, task["id"])
    assert found["running"] and "+def logout():" in found["diff"]
    store.update_task(task["id"], status="done")
    assert main(["peek", str(task["id"])]) == 1 and "not running" in capsys.readouterr().err


def test_texts_give_each_file_before_and_after_live_and_kept(git_repo, store):
    from locks import acp

    task = _running(store)
    store.update_task(task["id"], cwd=str(git_repo))
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    store.add_event(task["id"], "attempt_started", seq=1, base=base)
    path = home() / "worktrees" / f"task-{task['id']}"
    branch = f"locks/task-{task['id']}"
    git(git_repo, "worktree", "add", "-q", "-b", branch, str(path))
    (path / "logout.py").write_text("def logout():\n    return True\n")
    (path / "blob.bin").write_bytes(b"\0\1\2")  # a binary is no text to show
    found = progress.texts(path, base)
    assert found == {"files": [{"path": "logout.py", "old": None, "new": "def logout():\n    return True\n"}],
                     "more": 0}
    shown = acp.task_diffs(task["id"], True)
    assert shown == [{"type": "diff", "path": str(git_repo / "logout.py"), "oldText": None,
                      "newText": "def logout():\n    return True\n"}]
    # Once it ended, the branch it kept is what the editor sees.
    git(path, "add", "logout.py")
    git(path, "commit", "-qm", "logout")
    store.add_event(task["id"], "finished", status="done", branch=branch, base=base)
    store.update_task(task["id"], status="done")
    assert acp.task_diffs(task["id"], False)[0]["newText"].startswith("def logout")
    assert progress.of(store, {**task, "body": "Add:\n- a logout button\n- its test"})["criteria"][1] == {
        "id": "S2", "text": "its test"}


def _folder_of_two_repositories(tmp_path):
    folder = tmp_path / "folder"
    out = []
    for name in ("api", "web"):
        repo_dir = folder / name
        repo_dir.mkdir(parents=True)
        git(repo_dir, "init", "-q", "-b", "main")
        (repo_dir / "app.py").write_text("print('hi')\n")
        git(repo_dir, "add", "-A")
        git(repo_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        out.append(repo_dir)
    return folder, *out


def test_a_task_in_a_folder_of_repositories_is_read_in_each_of_them(tmp_path, store, capsys):
    """No worktree can be made in a folder that is no checkout: progress, peek and the phase read each
    repository against the commit it was at when the attempt started."""
    from locks.ui import api as ui

    from .test_checking import log

    folder, api, web = _folder_of_two_repositories(tmp_path)
    (web / "mine.txt").write_text("the person's, uncommitted\n")  # already there: not the worker's
    task = _running(store)
    places = isolate.stand(folder, [api, web])
    store.add_event(task["id"], "attempt_started", seq=1, places=places)
    assert [p["name"] for p in places] == ["api", "web"]
    assert [p["head"] for p in places] == [git(api, "rev-parse", "HEAD"), git(web, "rev-parse", "HEAD")]
    assert progress.changes(task["id"], None, places=places) == {"files": 0, "added": 0, "removed": 0, "paths": []}
    assert isolate.changed_so_far(home(), folder, task["id"], {"places": places}) == ()

    (api / "app.py").write_text("print('hi')\nprint('there')\n")  # tracked, modified
    (api / "__pycache__").mkdir()
    (api / "__pycache__" / "x.pyc").write_text("junk\n")  # a tool's artifact is no change
    (web / "page.ts").write_text("a\nb\nc\n")  # new
    (web / "mine.txt").write_text("the person's, uncommitted\n")  # touched by no one
    (web / "done.ts").write_text("z\n")
    git(web, "add", "done.ts")
    git(web, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "worker committed")
    log([(task["id"], 1, "bash:cat")])

    assert progress.changes(task["id"], None, places=places) == {
        "files": 3, "added": 5, "removed": 0, "paths": ["api/app.py", "web/done.ts", "web/page.ts"]}
    assert isolate.changed_so_far(home(), folder, task["id"], {"places": places}) \
        == ("api/app.py", "web/done.ts", "web/page.ts")
    now = progress.of(store, store.get_task(task["id"]))
    assert now["changes"]["files"] == 3
    assert now["phase"] == "implementing"  # a shell wrote: no write call
    assert progress.files_line(now) == "wrote api/app.py, web/done.ts, web/page.ts"

    found = progress.peek(task["id"], None, places=places)
    assert "# api" in found["diff"]
    assert "# web" in found["diff"]
    assert "mine.txt" not in found["diff"]
    assert "+print('there')" in found["diff"]
    assert "+++ b/web/page.ts" in found["diff"]
    assert not found["cut"]
    assert [p["head"] for p in found["places"]] == [p["head"] for p in places]

    assert main(["peek", str(task["id"])]) == 0
    assert "+++ b/api/app.py" in capsys.readouterr().out
    assert main(["peek", str(task["id"]), "--stat"]) == 0
    assert "3 files +5 −0" in capsys.readouterr().out
    assert "+++ b/web/page.ts" in ui.task_diff(store, task["id"])["diff"]
    # a task that recorded no places and has no worktree still says there is nothing to read
    bare = _running(store)
    assert progress.changes(bare["id"], None) is None
    assert progress.peek(bare["id"], None) is None


def test_a_checkout_git_cannot_read_says_nothing_rather_than_less(tmp_path, store, capsys):
    folder, api, web = _folder_of_two_repositories(tmp_path)
    task = _running(store)
    places = isolate.stand(folder, [api, web])
    store.add_event(task["id"], "attempt_started", seq=2, places=places)
    (api / "app.py").write_text("changed\n")
    shutil.rmtree(web)  # the checkout is gone: git cannot say
    assert progress.changes(task["id"], None, places=places) is None
    assert progress.peek(task["id"], None, places=places) is None
    assert isolate.changed_so_far(home(), folder, task["id"], {"places": places}) is None
    assert main(["peek", str(task["id"]), "--stat"]) == 1
    assert "no worktree of its own, or git cannot say" in capsys.readouterr().err


def test_reading_a_checkout_never_rewrites_its_index(tmp_path, store):
    """The checkout is the person's, and the worker or they may run `git add` in it while a view reads it:
    `git diff` would rewrite its index to refresh the stat data of a file touched and left as it was."""
    folder, api, web = _folder_of_two_repositories(tmp_path)
    task = _running(store)
    places = isolate.stand(folder, [api, web])
    store.add_event(task["id"], "attempt_started", seq=2, places=places)
    (api / "app.py").write_text("print('hi')\nprint('there')\n")
    (web / "new.ts").write_text("a\n")
    older = (web / "app.py").stat().st_mtime_ns - 5_000_000_000
    os.utime(web / "app.py", ns=(older, older))  # touched, content the same: stat-dirty
    indexes = [api / ".git" / "index", web / ".git" / "index"]
    before = [(i.stat().st_ino, i.stat().st_mtime_ns) for i in indexes]

    assert progress.changes(task["id"], None, places=places)["paths"] == ["api/app.py", "web/new.ts"]
    assert "+print('there')" in progress.peek(task["id"], None, places=places)["diff"]
    assert isolate.changed_so_far(home(), folder, task["id"], {"places": places}) == ("api/app.py", "web/new.ts")
    assert [(i.stat().st_ino, i.stat().st_mtime_ns) for i in indexes] == before
