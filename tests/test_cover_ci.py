"""Branches of `locks.ci` the CI suite leaves unread: broken manifests, deep scripts, the YAML
line reader's edges, workflow files that hold no jobs, and the reader of what the code reads."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from locks import ci
from locks.store import Store

from .conftest import git


def _workflow(top: Path, text: str, name: str = "ci.yml") -> None:
    folder = top / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def test_a_broken_package_json_names_no_script(tmp_path: Path):
    (tmp_path / "package.json").write_text("{ not json")
    assert ci.scripts(tmp_path) == {}


def test_a_deno_task_of_another_shape_is_left_out_and_dependencies_run_first(tmp_path: Path):
    (tmp_path / "deno.json").write_text(json.dumps({"tasks": {
        "odd": 3, "nameless": {"description": "no command"}, "lint": "deno lint",
        "check": {"command": "deno test", "dependencies": ["lint", 7]}}}))
    assert ci.scripts(tmp_path) == {"lint": "deno lint", "check": "deno task lint && deno test"}


def test_scripts_are_followed_only_so_deep(tmp_path: Path):
    chain = {f"s{n}": f"npm run s{n + 1}" for n in range(ci.DEPTH + 2)}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": chain}))
    found = ci.runs("npm run s0", tmp_path)
    assert found == ["npm run s0", *(f"npm run s{n}" for n in range(1, ci.DEPTH + 1))]


def test_a_deno_lock_without_a_browser_says_none(git_repo: Path):
    sub = git_repo / "web"
    sub.mkdir()
    (sub / "deno.lock").write_text(json.dumps({"version": "4", "specifiers": {"npm:zod@3": "3.22.0"}}))
    (git_repo / "deno.lock").write_text(json.dumps({"specifiers": {"npm:@playwright/test@1.40": "1.40.0"}}))
    assert ci._browser_dependency(sub) == "@playwright/test in deno.lock"
    (git_repo / "deno.lock").write_text("{}")
    assert ci._browser_dependency(sub) is None


def test_the_line_reader_refuses_an_unclosed_quote_and_trims_a_block_s_trailing_blanks():
    with pytest.raises(ci.Unreadable, match="an unclosed quote"):
        ci.read("name: 'never closed\n")
    assert ci.read("run: |\n  npm test\n  npm run lint\n\n\nnext: 1\n") == {
        "run": "npm test\nnpm run lint", "next": "1"}


def test_workflows_without_jobs_are_unreadable_and_a_job_of_another_shape_is_skipped(git_repo: Path):
    _workflow(git_repo, "name: nothing here\n", "empty.yml")
    _workflow(git_repo, "jobs:\n  odd: 3\n  test:\n    services:\n      db:\n        ports: x\n"
                        "      cache:\n        image: ''\n"
                        "      pg:\n        image: docker.io/library/postgres:16\n"
                        "    steps:\n      - run: npm test\n", "ci.yml")
    found, unreadable = ci.jobs(git_repo)
    assert unreadable == [".github/workflows/empty.yml"]
    assert [j.name for j in found] == ["test"]
    assert found[0].services == ("db", "cache", "postgres")


def test_a_workflow_folder_that_cannot_be_listed_reads_as_none(git_repo: Path, monkeypatch):
    _workflow(git_repo, "jobs:\n  test:\n    steps:\n      - run: npm test\n")

    def glob(self, pattern):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "glob", glob)
    assert ci.jobs(git_repo) == ([], [])


def test_what_the_code_reads_is_every_name_when_git_cannot_say(tmp_path: Path, git_repo: Path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ci._read_by_code(plain, {"DATABASE_URL", "not a name"}) == {"DATABASE_URL"}  # git refused: rc 128
    assert ci._read_by_code(git_repo, {"not a name"}) == set()
    (git_repo / "app.py").write_text("import os\nos.environ['DATABASE_URL']\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "env")
    assert ci._read_by_code(git_repo, {"DATABASE_URL", "UNUSED_FLAG"}) == {"DATABASE_URL"}

    def broken(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", broken)
    assert ci._read_by_code(git_repo, {"DATABASE_URL", "UNUSED_FLAG"}) == {"DATABASE_URL", "UNUSED_FLAG"}


def test_a_quiet_list_that_cannot_be_read_quiets_nothing(store: Store):
    store.set_meta(ci.QUIET_META + "r", "{not json")
    assert ci.quiet(store, "r") == set()


def test_a_pipeline_keeps_an_or_whole():
    assert ci._pipeline("make || echo failed | grep x") == ["make || echo failed", "grep x"]


def test_the_runner_near_a_change_is_found_past_an_assignment_alone():
    assert ci.related("CI=1 && pytest -q", None) == ("pytest", "python -m pytest -x {files}")
