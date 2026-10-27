"""The TRACER method carried into the work: the worker's brief and the independent check's."""
from __future__ import annotations

from cauce import tracer, workflows
from cauce.orchestrate import run

from .conftest import git
from .test_checking import check, passing
from .test_orchestrate import Script, kind

FLOW = """Flow: find training material by its title
Capability: find material
Success path
- WORKS: the search returns the file
- CORRECT: the file shows under its real name with a thumbnail
Failure paths
| Empty data | 200 | CORRECT: the screen says what it is for and the first thing to do |
"""


def _tracer(repo):
    folder = repo / "docs" / "tracer"
    (folder / "flows").mkdir(parents=True)
    (folder / "audiences.md").write_text("Learners: no technical words, never a UUID or a path.\n")
    (folder / "anti-patterns.md").write_text("- A storage key or UUID shown to a person.\n")
    (folder / "floors.md").write_text("File list: search by title, real names, empty state.\n")
    (folder / "flows" / "find-material.md").write_text(FLOW)
    (folder / "flows" / "track-progress.md").write_text("Flow: progress\n- CORRECT: a bar per course\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracer")


def test_flows_are_named_strictly_by_the_request(git_repo):
    _tracer(git_repo)
    assert [p.stem for p in tracer.flows_for(git_repo, "Add search to find training material by title")] == [
        "find-material"]
    assert tracer.flows_for(git_repo, "find the bug in the login") == []
    assert [p.stem for p in tracer.flows_for(git_repo, "learners finding the materials")] == ["find-material"]


def test_a_writing_worker_reads_the_method_and_the_check_holds_the_change_to_its_correct_lines(
        git_repo, store, monkeypatch):
    _tracer(git_repo)
    monkeypatch.setenv("CAUCE_VERIFIER", "on")
    script = Script(passing(), check("pass"), write="app.py")
    report = run("Fix the payment of the course so learners can find material", git_repo, store, registry={},
                 launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    worker, verifier = (s.prompt for s in script.specs)
    assert "follows the TRACER method" in worker and "never a UUID" in worker and "real name" in worker
    assert "track-progress" not in worker  # a flow the request does not name stays out
    assert "CORRECT lines of the flows this request names" in verifier
    assert "real name with a thumbnail" in verifier and "storage key or UUID" in verifier
    planned = store.last_event(report.task_id, "planned")["data"]
    assert any(r.startswith("tracer: ") for r in planned["reasons"])


def test_a_reading_task_or_the_setting_off_leaves_the_method_out(git_repo, store, monkeypatch):
    _tracer(git_repo)
    script = Script(passing("found it"))
    run("where do learners find material", git_repo, store, registry={}, launcher=script, classifier=kind("explore"))
    assert "TRACER" not in script.specs[0].prompt
    monkeypatch.setenv("CAUCE_TRACER", "off")
    script = Script(passing(), write="app.py")
    run("let learners find material", git_repo, store, registry={}, launcher=script, classifier=kind("implement"))
    assert "TRACER" not in script.specs[0].prompt


def test_a_check_that_has_layout_inspector_is_told_to_measure_the_floors_with_it(git_repo):
    _tracer(git_repo)
    told = "\n".join(tracer.verifier_lines(git_repo, "find material", ("layout-inspector",)))
    assert "accessibility_spatial" in told and "compare_viewports" in told
    assert "accessibility_spatial" not in "\n".join(tracer.verifier_lines(git_repo, "find material"))


def test_without_the_folder_nothing_is_said(git_repo):
    assert tracer.brief_lines(git_repo, "find material") == [] and tracer.verifier_lines(git_repo, "x") == []


def test_the_bundled_tracer_slice_workflow_is_a_valid_definition(tmp_path):
    found = workflows.load("tracer-slice", tmp_path)
    assert found["scope"] == "bundled" and found["inputs"] == ["slice"]
    assert [s["id"] for s in found["steps"]] == ["build", "verify", "refute", "fix", "recheck"]
