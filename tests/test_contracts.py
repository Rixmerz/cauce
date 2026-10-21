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
