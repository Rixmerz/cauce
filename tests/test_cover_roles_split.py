"""Branches of `locks.roles` and `locks.split` their suites leave unread."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from locks import classify, roles, split

from .conftest import git

# --- split ---------------------------------------------------------------------------------------


def test_a_proposal_reads_as_json_and_a_file_at_the_top_names_no_surface():
    got = split.by_items("Do two things:\n- fix README.md\n- add src/cart/totals.py")
    assert [p["surfaces"] for p in got.parts] == [[], ["src/cart/**"]]
    data = json.loads(split.as_json(got))
    assert data["source"] == "items" and len(data["parts"]) == 2 and data["problem"] == ""


def test_the_layout_counts_tracked_files_per_top_folder(git_repo: Path):
    (git_repo / "src").mkdir()
    (git_repo / "src" / "a.py").write_text("x = 1\n")
    (git_repo / "src" / "b.py").write_text("x = 2\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "src")
    assert split.layout(git_repo) == ["(root: 1 files)", "src/ (2 files)"]

    def broken(*a, **kw):
        raise OSError("no git")

    assert split.layout(git_repo, run=broken) == []


def test_without_an_ask_the_split_goes_to_haiku(monkeypatch):
    asked = []

    def haiku(system, schema, question):
        asked.append(question)
        return None, 0.002, "haiku is down"

    monkeypatch.setattr(classify, "ask_haiku", haiku)
    got = split.by_model("Add the control and fix the overlaps", None)
    assert got.problem == "the model gave no split: haiku is down" and got.cost_usd == 0.002
    assert "The repository's layout:\n(unknown)" in asked[0]


# --- roles ---------------------------------------------------------------------------------------


def test_a_folder_git_cannot_list_is_walked(tmp_path: Path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x")
    real = subprocess.run

    def run(argv, *a, **kw):
        if argv[:2] == ["git", "ls-files"]:
            raise OSError("no git")
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    assert roles.project_files(tmp_path) == ["src/a.py"]


def test_a_bundled_skill_that_cannot_be_read_is_left_out(monkeypatch):
    every = roles.bundled_skills()
    assert every
    gone = every[0].name
    real = roles.find_skill
    monkeypatch.setattr(roles, "find_skill", lambda name, env=None: None if name == gone else real(name, env))
    assert gone not in [s.name for s in roles.bundled_skills()]
    assert len(roles.bundled_skills()) == len(every) - 1


def test_a_role_with_no_instructions_of_its_own_is_briefed_by_its_description():
    role = roles.Role("bare", "builds things", ("implement",), "")
    briefing = roles.brief(role)
    assert briefing.system_prompt == "You work as the `bare` role: builds things"


def test_a_role_rendered_with_no_origin_names_none():
    text = roles.render(roles.Role("bare", "builds things", ("implement",), "Do it well."))
    assert "derived_from" not in text
    assert text.endswith("---\n\nDo it well.\n")


def test_an_agent_file_that_is_no_agent_is_none():
    assert roles.from_agent("---\nname: x\n", fallback="f") is None  # never closed
    assert roles.from_agent('---\nname: "!!!"\n---\nDo the work.\n', fallback="!!!") is None


def test_importing_agents_skips_what_cannot_be_read_or_parsed(tmp_path: Path):
    with pytest.raises(roles.RoleError, match="no agent file at"):
        roles.import_agents(tmp_path / "nowhere")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "folder.md").mkdir()
    (agents / "listy.md").write_text("---\nname: listy\ndescription: [a, b]\n---\nDo the work.\n")
    (agents / "good.md").write_text("---\nname: good-builder\ndescription: builds things\n---\nDo the work.\n")
    written, skipped = roles.import_agents(agents, dry_run=True)
    assert [n for n, _ in written] == ["good-builder"]
    why = dict(skipped)
    assert why[str(agents / "folder.md")].startswith("unreadable: ")
    assert why[str(agents / "listy.md")] == "`description` is text"
