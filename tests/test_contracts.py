"""What one place writes and another reads back: each pair round-trips, or the bug is that one
of them drifted (a pause reason written `task #N:` and released as `task #N `, a resume that
rewrote a task's options without its workflow part)."""
import dataclasses
import json
import re
from pathlib import Path

from cauce import flow
from cauce.matrix import Cell
from cauce.orchestrate import Options

SRC = Path(__file__).resolve().parents[1] / "src" / "cauce"


def test_every_option_a_resume_stores_reads_back_the_same():
    full = Options(max_attempts=3, verify="make check", kind="feature", start=Cell("opus", "high"),
                   allow_approval=True, livespec=False, allow_tools=("Bash(x:*)",), isolate=False, max_turns=99,
                   base="abc123", role="architect", step_role="builder", surfaces=("api/**",),
                   ledger={"role": "fix", "round": 1, "ids": ["F1"]}, keep_learned=False, carry=True,
                   add_dirs=("/other",), services=("serve@8000",), run_verify="make test", parts=True,
                   integrate={"branches": [{"part": "api", "task": 2, "branch": "b"}], "contract": "c"},
                   touches=("ui",), reviewed_next=True)
    back = flow.queued_options({"options": json.dumps(full.stored())}, Options())
    lost = [f.name for f in dataclasses.fields(Options)
            if f.name != "use_model_classifier" and getattr(back, f.name) != getattr(full, f.name)
            and getattr(full, f.name) != getattr(Options(), f.name)]
    # Run-time fields no queued task carries are not stored; everything a task is queued with is.
    assert set(lost) <= RUN_TIME, lost


def test_the_notes_a_brief_carried_are_the_ones_weighing_reads_back(git_repo, store, monkeypatch):
    """`orchestrate` keeps on `briefed` the notes the worker was shown; `notes.weigh` proves or
    drops a candidate from that field alone."""
    from cauce import notes, repo
    from cauce.orchestrate import run

    from .test_orchestrate import Script, kind, ok

    monkeypatch.setenv("CAUCE_NOTES", "on")
    key = repo.key(git_repo)
    note = store.add_note(key, "business", "Prices in cents", "Prices are stored in cents in every table",
                          author="worker", filed_by="haiku", state=notes.CANDIDATE)
    script = Script(ok())
    report = run("change how prices are stored in cents", git_repo, store, registry={}, launcher=script,
                 classifier=kind("implement"), notes_ask=lambda *a, **kw: (None, 0.0, ""))
    assert f"#{note} [business] Prices in cents" in script.specs[0].prompt and "(unproven:" in script.specs[0].prompt
    assert store.last_event(report.task_id, "briefed")["data"][notes.BRIEFED] == [note]
    assert notes.carried(store) == {report.task_id: {note}}


#: Options a run is given by whoever starts it, never kept on the task.
RUN_TIME: set[str] = set()


def test_every_reason_a_lane_is_paused_with_is_one_its_task_releases(store):
    """Each `pause_lane` reason in the code starts with `task #N` followed by a space or a
    colon, and a release of N lifts both: a new format is checked here, not found in use."""
    reasons = re.findall(r'pause_lane\([^,]+,\s*f"(task #\{[^}]+\}[^"]{0,2})', "".join(
        p.read_text() for p in SRC.rglob("*.py")))
    assert len(reasons) >= 4
    for n, reason in enumerate(reasons, 1):
        written = re.sub(r"\{[^}]+\}", str(n), reason, count=1) + " the rest"
        assert re.match(rf"task #{n}[ :]", written), reason
        store.pause_lane(f"r{n}", written)
        assert store.release_lane(f"r{n}", n), reason


def test_a_brief_s_revision_arm_is_what_the_trial_reads_back(store):
    """`revisions.for_brief` writes the arm on the `briefed` event; `revisions.arms` counts it,
    reading a task's first brief (a resume's is not the one its first try ran under)."""
    from cauce import revisions

    cand = store.add_role_revision(at="2026-10-01T00:00:00+00:00", repo="r", role="implementer",
                                   record="proposed", hash="h", lessons=["Run the suite before a pass."],
                                   version="0.32.0")
    task = store.create_task("t", status="done", source="cauce", repo="r")
    _, data = revisions.for_brief(store, "r", "implementer", task["id"])
    store.add_event(task["id"], "briefed", role="implementer", **data)
    store.add_event(task["id"], "briefed", role="implementer", revision_trial=cand, revision_arm="nonsense")
    store.add_attempt(task["id"], cell="sonnet/medium", max_turns=80, passed=1)
    found = revisions.arms(store, "r", cand)
    assert found[data["revision_arm"]]["tasks"] == 1 and found[data["revision_arm"]]["briefed"] == 1
    assert sum(arm["briefed"] for arm in found.values()) == 1


def test_a_check_that_failed_only_as_at_the_base_reads_back_in_the_ending(git_repo, store):
    """`orchestrate` writes `check_preexisting` and the verdict card's `check.state`; the ending
    notice reads both back (`hooks.verdict_lines`, `hooks._found_before`) from those fields alone."""
    from cauce import hooks
    from cauce.orchestrate import FAILING_AT_BASE, run

    from .test_orchestrate import Script, kind, ok

    report = run("add the export", git_repo, store, Options(verify="echo 'FAIL old.test.js'; exit 1"), registry={},
                 launcher=Script(ok(), write="f.py"), classifier=kind("implement"))
    check = store.final_event(report.task_id)["data"]["verdict"]["check"]
    kept = store.last_event(report.task_id, "check_preexisting")["data"]
    assert check["state"] == FAILING_AT_BASE and check["base"] == kept["base"] and check["failures"] == kept["n"]
    assert any(FAILING_AT_BASE in line and kept["base"][:12] in line for line in hooks.verdict_lines(
        {"check": check}))
    offered = hooks._found_before(store, store.get_task(report.task_id))
    assert kept["failures"][0] in offered[1] and f"base {kept['base'][:12]}" in offered[1]
