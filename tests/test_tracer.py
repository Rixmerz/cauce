"""The TRACER method carried into the work: the worker's brief and the independent check's."""
from __future__ import annotations

from cauce import config, tracer, workflows
from cauce.orchestrate import run

from .conftest import git
from .test_checking import check, passing
from .test_orchestrate import Script, kind
from .test_plugins import LAYOUT, install

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


FLOORS = """- Every control is a 44 px target on a phone.
- Text on the card never overflows or is cut off.
- Layout holds at every breakpoint the audience uses.
- Body text keeps WCAG contrast.
- A storage key never shows.
"""


def _floors(repo):
    _tracer(repo)
    (repo / "docs" / "tracer" / "floors.md").write_text(FLOORS)
    git(repo, "commit", "-q", "-am", "floors")  # a worktree reads what is committed


def _beside(told, text):
    """The tool text on the line of `told` that holds `text`."""
    return next(line for line in told.splitlines() if text in line)


def test_a_check_that_has_layout_inspector_is_told_which_tool_measures_each_floor_and_anti_pattern(git_repo):
    _floors(git_repo)
    told = "\n".join(tracer.verifier_lines(git_repo, "find material", ("layout-inspector",)))
    assert "accessibility_spatial" in _beside(told, "44 px target")
    assert "detect_issues" in _beside(told, "never overflows")
    assert "compare_viewports" in _beside(told, "every breakpoint")
    assert "no tool measures it" in _beside(told, "WCAG contrast")
    assert "accessibility_spatial" not in _beside(told, "WCAG contrast")
    assert "2 of the floor and anti-pattern lines name nothing layout-inspector measures" in told
    assert "storage key or UUID" in told  # the catalogue's own line is still the check's to hold
    assert "accessibility_spatial" not in "\n".join(tracer.verifier_lines(git_repo, "find material"))


def test_floors_alone_are_measured_but_say_nothing_without_a_measuring_neighbour(git_repo):
    folder = git_repo / "docs" / "tracer"
    folder.mkdir(parents=True)
    (folder / "floors.md").write_text(FLOORS)
    assert tracer.verifier_lines(git_repo, "x") == []
    assert "compare_viewports" in "\n".join(tracer.verifier_lines(git_repo, "x", ("layout-inspector",)))


def test_a_ui_change_gets_layout_inspector_in_its_check_with_the_method_mapped(git_repo, store, monkeypatch):
    _floors(git_repo)
    install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    monkeypatch.setenv("CAUCE_VERIFIER", "on")
    script = Script(passing(), check("pass"), write="Cart.tsx")
    report = run("fix the cart colours", git_repo, store, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    verifier = script.specs[1]
    assert "layout-inspector" in verifier.mcp_servers
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt
    assert "Floors and anti-patterns, with the tool that measures each" in verifier.prompt
    assert "compare_viewports" in _beside(verifier.prompt, "every breakpoint")


def test_a_ui_change_gets_layout_inspector_in_its_check_without_the_method(git_repo, store, monkeypatch):
    install("layout-inspector", "0.4.0", manifest={"mcpServers": "./.claude-plugin/mcp.json"},
            files={".claude-plugin/mcp.json": LAYOUT}, skill="layout-inspection")
    monkeypatch.setenv("CAUCE_VERIFIER", "on")
    script = Script(passing(), check("pass"), write="Cart.tsx")
    report = run("fix the cart colours", git_repo, store, launcher=script, classifier=kind("implement"))
    assert report.status == "done"
    verifier = script.specs[1]
    assert "layout-inspector" in verifier.mcp_servers and "mcp__layout-inspector" in verifier.allowed_tools
    assert "layout-inspector: measure the rendered page" in verifier.append_system_prompt
    assert "TRACER" not in verifier.prompt and "Floors and anti-patterns" not in verifier.prompt
    planned = store.last_event(report.task_id, "planned")["data"]["reasons"]
    assert not any(r.startswith("layout-inspector: ") for r in planned)


def test_the_plan_says_once_why_a_ui_change_is_checked_without_layout_inspector(git_repo, store):
    # An empty registry is the plugin absent, whatever this machine has installed.
    first = run("fix the cart colours in Cart.tsx", git_repo, store, registry={},
                launcher=Script(passing(), write="Cart.tsx"), classifier=kind("implement"))
    said = [r for r in store.last_event(first.task_id, "planned")["data"]["reasons"]
            if r.startswith("layout-inspector: ")]
    assert len(said) == 1 and "not installed" in said[0]
    again = run("fix the cart colours in Cart.tsx once more", git_repo, store, registry={},
                launcher=Script(passing(), write="Cart.tsx"), classifier=kind("implement"))
    planned = store.last_event(again.task_id, "planned")["data"]
    assert said[0] in planned["reasons"] and said[0] in planned["quiet"]  # kept on the event, not said again
    config.save({"plugins": False})
    off = run("fix the layout of Cart.tsx", git_repo, store, registry={}, launcher=Script(passing(), write="Cart.tsx"),
              classifier=kind("implement"))
    assert any("the `plugins` setting is off" in r for r in store.last_event(off.task_id, "planned")["data"]["reasons"])


def test_the_plan_says_nothing_of_layout_inspector_for_work_that_is_no_ui(git_repo, store):
    report = run("fix the cart total in app.py", git_repo, store, registry={},
                 launcher=Script(passing(), write="app.py"), classifier=kind("implement"))
    assert not any(r.startswith("layout-inspector: ")
                   for r in store.last_event(report.task_id, "planned")["data"]["reasons"])


def test_without_the_folder_nothing_is_said(git_repo):
    assert tracer.brief_lines(git_repo, "find material") == [] and tracer.verifier_lines(git_repo, "x") == []


def test_the_bundled_tracer_slice_workflow_is_a_valid_definition(tmp_path):
    found = workflows.load("tracer-slice", tmp_path)
    assert found["scope"] == "bundled" and found["inputs"] == ["slice"]
    assert [s["id"] for s in found["steps"]] == ["build", "verify", "refute", "fix", "recheck"]
