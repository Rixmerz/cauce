"""What a failed check said, kept per attempt (`launch.check_tail`): shown by `locks show`, the
board's task page and the ending, and handed to the next attempt's brief."""
from __future__ import annotations

import pytest

from locks import cli, hooks, launch, orchestrate
from locks.escalate import Attempt
from locks.matrix import Cell
from locks.orchestrate import Options, run
from locks.store import Store
from locks.ui import api

from .test_orchestrate import Script, kind, ok

ESC = "\x1b"
RED = "FAIL src/a.test.js"
CHECK = f"echo '{RED}'; echo '  expected 3 to be 2'; exit 1"
# Red only with the worker's file, so the base passes and the failure is the work's, not preexisting.
WORK_BREAKS = f"[ ! -f out.txt ] || {{ echo '{RED}'; echo '  expected 3 to be 2'; exit 1; }}"


def _long_run(failing: int = 12, noise: int = 200) -> str:
    """A suite's output: failing names early, a long passing body, a summary at the end."""
    head = [f"FAIL src/f{n}.test.js" for n in range(failing)]
    body = [f"  ✓ case {n} passes" for n in range(noise)]
    return "\n".join([*head, *body, "Tests: 12 failed, 200 passed", "  expected 3 to be 2"])


# --- the pure reader ---------------------------------------------------------------------


def test_a_short_output_is_kept_whole_with_colours_and_progress_redraws_dropped():
    out = f"{ESC}[31mFAIL{ESC}[39m src/a.test.js\n10%\r50%\r100%\n  expected 3 to be 2\n\n\n"
    assert launch.check_tail(out) == "FAIL src/a.test.js\n100%\n  expected 3 to be 2"


def test_the_directories_the_check_ran_in_are_taken_off_the_paths(tmp_path):
    out = f"FAIL {tmp_path}/src/a.test.js\nError at {tmp_path}"
    assert launch.check_tail(out, roots=(tmp_path,)) == "FAIL src/a.test.js\nError at ."


def test_secrets_are_masked_and_private_text_is_left_out():
    out = "token sk-abcdefgh12345678 leaked\npassword: hunter2\n<private>my own words</private>\nFAIL a.test.js"
    tail = launch.check_tail(out, note="compared: ghp_abcdefgh12345678")
    assert "sk-abcdefgh12345678" not in tail
    assert "hunter2" not in tail
    assert "my own words" not in tail
    assert "ghp_abcdefgh12345678" not in tail
    assert "[secret]" in tail
    assert "FAIL a.test.js" in tail


def test_a_long_output_keeps_the_failing_names_first_and_its_last_lines():
    tail = launch.check_tail(_long_run())
    assert len(tail) <= launch.CHECK_TAIL_CHARS
    assert tail.startswith("failing (12):\n  FAIL src/f0.test.js")
    assert "and 4 more" in tail
    assert "last lines of the output:\n…\n" in tail
    assert tail.rstrip().endswith("Tests: 12 failed, 200 passed\n  expected 3 to be 2")
    assert "case 0 passes" not in tail


def test_the_comparison_with_the_base_follows_the_output_and_is_bounded():
    tail = launch.check_tail("FAIL a.test.js", note="1 failing line(s) are new with this work\n" + "x " * 2000)
    assert tail.startswith("FAIL a.test.js\n\n1 failing line(s) are new")
    assert len(tail) <= launch.CHECK_TAIL_CHARS


def test_one_enormous_last_line_is_clipped_to_the_room_not_dropped():
    tail = launch.check_tail("FAIL a.test.js\n" + "y" * 50_000)
    assert 0 < len(tail) <= launch.CHECK_TAIL_CHARS
    assert "y" in tail


def test_an_output_with_nothing_in_it_keeps_nothing():
    assert launch.check_tail("") == ""
    assert launch.check_tail(f"{ESC}[0m\n  \n", note="a note alone says nothing of the output") == ""


# --- the check locks runs ---------------------------------------------------------------


def test_a_red_check_keeps_its_tail_and_a_green_one_keeps_none(tmp_path):
    red = orchestrate._verified(ok(), CHECK, tmp_path)
    assert red.failure is orchestrate.Failure.CODE_BUG
    assert red.check_tail == f"{RED}\n  expected 3 to be 2"
    assert orchestrate._verified(ok(), "true", tmp_path).check_tail == ""


def test_a_check_that_could_not_run_keeps_what_it_said_too(tmp_path):
    lost = orchestrate._verified(ok(), "echo 'npm ERR! code ENOTFOUND registry.npmjs.org'; exit 1", tmp_path)
    assert lost.failure is orchestrate.Failure.ENVIRONMENT
    assert "ENOTFOUND registry.npmjs.org" in lost.check_tail


# --- from the run to every place that shows it --------------------------------------------


@pytest.fixture
def failed(git_repo, store: Store):
    """A task whose check fails on both its attempts, in a session that sent it."""
    script = Script(ok(), ok(), write="out.txt")
    report = run("add the thing", git_repo, store, Options(verify=WORK_BREAKS, max_attempts=2), registry={},
                 launcher=script, classifier=kind("implement"), session_id="s")
    assert report.status != "done"
    return report, script


def test_the_next_attempt_starts_from_the_actual_failure(failed):
    _, script = failed
    assert len(script.specs) == 2
    assert "What locks' own check said when it failed the last attempt" in script.specs[1].prompt
    assert f"{RED}\n  expected 3 to be 2" in script.specs[1].prompt


def test_each_attempt_keeps_the_tail_on_its_finished_event(failed, store: Store):
    report, _ = failed
    tails = store.check_tails(report.task_id)
    assert sorted(tails) == [1, 2]
    for tail in tails.values():
        assert tail.startswith(f"{RED}\n  expected 3 to be 2\n\n")
        assert "the same check passes: every failure here is this work's" in tail  # locks' comparison with the base


def test_show_prints_the_check_s_output_under_its_attempt(failed, capsys):
    report, _ = failed
    assert cli.main(["show", str(report.task_id)]) == 0
    lines = capsys.readouterr().out.splitlines()
    at = lines.index("    the check's own output:")
    assert lines[at + 1:at + 3] == [f"      {RED}", "        expected 3 to be 2"]


def test_the_task_page_is_sent_the_tail_of_each_attempt(failed, store: Store):
    report, _ = failed
    attempts = api.task_detail(store, report.task_id)["attempts"]
    assert [a["check_tail"] for a in attempts] == list(store.check_tails(report.task_id).values())
    assert all(a["check_tail"].startswith(RED) for a in attempts)


def test_the_ending_of_a_failed_task_shows_the_check_s_output(failed, store: Store):
    report, _ = failed
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert f"#{report.task_id} [failed]" in notice or f"#{report.task_id} [blocked]" in notice
    assert "the check's own output on its last attempt" in notice
    assert f"    {RED}\n      expected 3 to be 2" in notice


def test_an_ending_never_shows_an_earlier_run_s_check_output(store: Store):
    t = store.create_task("old", status="blocked", source="locks", repo="r", session_id="s")
    store.add_event(t["id"], "attempt_finished", seq=1, check_tail="FAIL old.test.js")
    store.add_event(t["id"], "planned", ladder=["haiku"], start="haiku", reasons=[])
    store.add_event(t["id"], "finished", status="blocked", reason="no sign of life")
    current = store.create_task("new", status="blocked", source="locks", repo="r", session_id="s")
    store.add_event(current["id"], "planned", ladder=["haiku"], start="haiku", reasons=[])
    store.add_event(current["id"], "attempt_finished", seq=1, check_tail="FAIL new.test.js")
    store.add_event(current["id"], "finished", status="blocked", reason="red")
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "FAIL old.test.js" not in notice
    assert "FAIL new.test.js" in notice


def test_an_ending_clips_what_it_shows_and_the_show_keeps_all(store: Store, capsys):
    t = store.create_task("big", status="blocked", source="locks", repo="r", session_id="s")
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=0, failure="code_bug", summary="red")
    store.add_event(t["id"], "planned", ladder=["haiku"], start="haiku", reasons=[])
    whole = launch.check_tail(_long_run())
    store.add_event(t["id"], "attempt_finished", seq=1, check_tail=whole)
    store.add_event(t["id"], "finished", status="blocked", reason="red")
    notice = hooks.endings(store, "s", claim="stop")[0]
    assert "FAIL src/f0.test.js" in notice
    assert "Tests: 12 failed" not in notice
    assert cli.main(["show", str(t["id"])]) == 0
    assert "Tests: 12 failed, 200 passed" in capsys.readouterr().out


def test_a_resume_hands_the_next_worker_the_tail_its_history_kept(store: Store):
    t = store.create_task("resume me", status="blocked", source="locks", repo="r")
    store.add_attempt(t["id"], cell="sonnet/high", max_turns=30, passed=0, failure="code_bug", summary="red")
    store.add_event(t["id"], "attempt_finished", seq=1, check_tail=f"{RED}\n  expected 3 to be 2")
    last = Attempt(Cell.parse("sonnet/high"), 30, False, orchestrate.Failure.CODE_BUG, "red",
                   check_tail=store.check_tails(t["id"])[1])
    assert f"{RED}\n  expected 3 to be 2" in orchestrate.failed_output(last)
    assert orchestrate.continuation_note(last) == orchestrate.failed_output(last)


def test_an_attempt_from_before_the_tail_was_kept_still_briefs_its_evidence():
    old = Attempt(Cell.parse("sonnet/high"), 30, False, orchestrate.Failure.CODE_BUG, "red", evidence="2 tests failed")
    assert orchestrate.failed_output(old) == "The output that failed the last attempt, last lines:\n2 tests failed"
    assert orchestrate.failed_output(Attempt(Cell.parse("sonnet/high"), 30, True, check_tail="x")) == ""
