"""Branches of `locks.tracer` and `locks.specs` their suites leave unread."""
from __future__ import annotations

import subprocess
from pathlib import Path

from locks import specs, tracer

from .conftest import git
from .test_tracer import FLOORS, _floors, _tracer

# --- tracer --------------------------------------------------------------------------------------


def test_a_brief_too_small_for_the_method_names_the_rest_by_section_up_to_its_bound(git_repo: Path, monkeypatch):
    _tracer(git_repo)
    folder = git_repo / "docs" / "tracer"
    (folder / "identity.md").write_text("# Identity\n\n" + "Plain words. " * 40 + "\n\n## Voice\n\nCalm.\n")
    (folder / "floors.md").write_text("# Floors\n\n## Lists\n\nReal names.\n\n## Empty\n\nSay what to do.\n")
    monkeypatch.setattr(tracer, "BRIEF_CHARS", 260)
    told = "\n".join(tracer.brief_lines(git_repo, "find training material by title"))
    assert "Not shown above, by section (read the lines you need):" in told
    assert "- Floors (docs/tracer/floors.md:1-9)" in told
    assert "- Anti-pattern catalogue (docs/tracer/anti-patterns.md)" in told  # no heading: named whole
    monkeypatch.setattr(tracer, "OUTLINE_CHARS", 45)
    told = "\n".join(tracer.brief_lines(git_repo, "find training material by title"))
    index = told.split("Not shown above, by section (read the lines you need):\n", 1)[1]
    assert index == "-   Voice (docs/tracer/identity.md:5-7)\n5 more section(s) in `docs/tracer/`."


def test_a_bare_correct_head_takes_the_line_under_it_whole():
    assert tracer.correct_lines("- CORRECT:\n    the file shows under its real name\n") == [
        "the file shows under its real name"]


def test_a_floor_line_of_no_words_is_no_requirement_and_all_measured_say_nothing_unmatched(git_repo: Path):
    _tracer(git_repo)
    folder = git_repo / "docs" / "tracer"
    (folder / "floors.md").write_text("| --- | --- |\n- Every control is a 44 px target on a phone.\n")
    (folder / "anti-patterns.md").write_text("- Text on the card never overflows or is cut off.\n")
    lines = tracer._measured_lines(git_repo, "layout-inspector")
    assert len(lines) == 2
    assert not any("name nothing" in line for line in lines)


def test_measured_lines_that_do_not_fit_are_counted(git_repo: Path, monkeypatch):
    _floors(git_repo)
    monkeypatch.setattr(tracer, "BRIEF_CHARS", 300)
    lines = tracer._measured_lines(git_repo, "layout-inspector")
    assert any(line.endswith("`docs/tracer/anti-patterns.md`.") and "did not fit here" in line for line in lines)
    assert FLOORS  # the floors the fixture wrote


def test_a_check_without_a_known_measurer_or_with_nothing_to_measure_adds_no_mapping(git_repo: Path):
    _tracer(git_repo)
    told = tracer.verifier_lines(git_repo, "find training material by title", ("no-such-neighbour",))
    assert not any("with the tool that measures each" in line for line in told)
    assert any(line.startswith("CORRECT lines of the flows") for line in told)
    folder = git_repo / "docs" / "tracer"
    (folder / "floors.md").unlink()
    (folder / "anti-patterns.md").unlink()
    told = tracer.verifier_lines(git_repo, "find training material by title", ("layout-inspector",))
    assert tracer.measured_by("layout-inspector") in told
    assert not any("with the tool that measures each" in line for line in told)


# --- specs ---------------------------------------------------------------------------------------


def test_specs_never_committed_say_so(git_repo: Path):
    (git_repo / "openspec").mkdir()
    found = specs.drift(git_repo)
    assert found is not None and found.last is None and found.code_commits == 1 and found.behind
    assert found.text() == "`openspec/` was never committed"


def test_a_checkout_with_no_commit_has_no_drift(tmp_path: Path):
    empty = tmp_path / "empty"
    (empty / "openspec").mkdir(parents=True)
    git(empty, "init", "-q", "-b", "main")
    assert specs.drift(empty) is None


def test_git_that_cannot_run_reads_as_nothing(tmp_path: Path, monkeypatch):
    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert specs._git(tmp_path, "log") is None
    assert specs._git_text(tmp_path, "log") is None


def test_a_spec_file_the_change_deleted_names_no_requirement(git_repo: Path):
    assert specs.unlinked(git_repo, None, ["openspec/specs/auth/spec.md"]) == []
