from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cauce import cli, survey
from cauce.store import home

from .conftest import git


def user(text, cwd, *, ts="2026-10-01T10:00:00Z", **extra):
    return {"type": "user", "cwd": str(cwd), "sessionId": "s", "gitBranch": "main", "timestamp": ts,
            "message": {"role": "user", "content": text}, **extra}


def assistant(blocks, *, ts="2026-10-01T10:01:00Z", model="claude-sonnet-5-5"):
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    return {"type": "assistant", "timestamp": ts, "message": {"model": model, "content": blocks}}


def tool(id_, name, **args):
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


def result(id_, text, *, error=False, ts="2026-10-01T10:30:00Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": id_, "is_error": error, "content": text}]}}


def write(claude: Path, slug: str, sid: str, entries, *, age_days: float = 0) -> Path:
    path = claude / "projects" / slug / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + "not json\n")
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def history_repo(repo: Path) -> None:
    for n, (subject, body, name) in enumerate([
        ("feat(cart): add totals", "", "cart.py"),
        ("fix(cart): round once", "Co-Authored-By: Claude <noreply@anthropic.com>", "cart.py"),
        ("fix: the total again", "", "cart.py"),
        ('Revert "feat(cart): add totals"', "", "cart.py"),
        ("arreglar el login", "", "login.py"),
    ]):
        (repo / name).write_text(f"{n}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", subject, *(["-m", body] if body else []))
    git(repo, "branch", "feat/x")
    git(repo, "tag", "v1")


def session(cwd):
    """A session that plans, builds, fails a test, fixes it, is corrected and ends in a commit and a PR."""
    return [
        user("Planifica la migración del carrito a centavos", cwd, ts="2026-10-01T10:00:00Z"),
        assistant([tool("p1", "ExitPlanMode", plan="x")]),
        user("implementa el cambio en cart.py", cwd),
        assistant([tool("w1", "Edit", file_path=f"{cwd}/cart.py", old_string="a", new_string="b"),
                   tool("t1", "Bash", command=f"cd {cwd} && uv run pytest -q tests/")]),
        result("w1", "ok"),
        result("t1", "Exit code 1\n=== FAILURES ===\nAssertionError: total 1.0 != 100 at /x/cart.py:3", error=True),
        assistant([tool("t2", "Bash", command="uv run pytest -q")]),
        result("t2", "Exit code 1\nAssertionError: total 2.0 != 100", error=True),
        assistant("The total is in dollars; it should be cents. I'll convert in cart.py."),
        assistant([tool("w2", "Write", file_path=f"{cwd}/cart.py", content="x"),
                   tool("t3", "Bash", command="uv run pytest -q")]),
        result("t3", "3 passed"),
        user("no, eso no: te dije que no toques el redondeo", cwd),
        user("sigue", cwd),
        user("[Request interrupted by user]", cwd),
        user("<command-name>/cauce:workflow</command-name>", cwd),
        assistant([tool("x1", "Bash", command="rm -rf build")]),
        result("x1", "The user doesn't want to proceed with this tool use.", error=True),
        assistant([tool("e1", "Edit", file_path=f"{cwd}/cart.py", old_string="zz", new_string="b")]),
        result("e1", "String to replace not found", error=True),
        assistant([tool("n1", "Bash", command="npm run build")]),
        result("n1", "npm ERR! Missing script: build", error=True),
        assistant([tool("s1", "Skill", skill="python-rules"), tool("a1", "Agent", subagent_type="Explore"),
                   tool("a2", "Task"), tool("c1", "Bash", command="git commit -m 'fix: cents'"),
                   tool("c2", "Bash", command="git push -u origin HEAD"),
                   tool("c3", "mcp__github__create_pull_request", title="x")]),
        user("x" * 2100 + " log pegado", cwd, ts="2026-10-01T11:00:00Z"),
        user("sidechain text", cwd, isSidechain=True),
    ]


def test_the_survey_reads_sessions_by_project_with_their_history(git_repo, tmp_path, store):
    history_repo(git_repo)
    claude = tmp_path / "claude-config"
    write(claude, "-repo", "s1", session(git_repo))
    write(claude, "-repo", "s2", [user("revisa el carrito #review", git_repo), assistant("ok")])
    write(claude, "-home", "s3", [user("hola", Path.home())])
    worker = home() / "worktrees" / "task-1"
    worker.mkdir(parents=True)
    write(claude, "-worker", "s4", [user("brief", worker)])
    write(claude, "-old", "s5", [user("old", git_repo)], age_days=400)
    write(claude, "-empty", "s6", [assistant("only the agent")])
    write(claude, "-nowhere", "s7", [{"type": "user", "message": {"content": "no cwd here"}}])
    gone = tmp_path / "gone"
    write(claude, "-gone", "s8", [user("from a folder since removed", gone)])

    found = survey.gather(store)
    read = found["read"]
    assert read["projects"] == 2 and read["sessions"] == 3
    assert read["skipped"] == {"older": 1, "not a project": 2, "empty": 1, "no directory": 1}
    p = next(x for x in found["projects"] if x["exists"])
    assert p["sessions"] == 2 and p["dir"] == str(git_repo)
    assert p["kinds"]["plan"] == 1 and p["kinds"]["review"] == 1 and p["kinds"]["implement"] == 1
    assert p["openers"][-1].startswith("Planifica la migración")  # newest session first
    shapes = {s["shape"] for s in p["shapes"]}
    assert any(s.startswith("plan → implement") and s.endswith("check → commit → pr") for s in shapes)
    f = p["friction"]
    assert (f["corrections"], f["continuations"], f["interrupts"], f["refusals"], f["pastes"]) == (1, 1, 1, 1, 1)
    assert "te dije" in f["samples"][0]["request"] and "cents" in f["samples"][0]["after"]
    fix = p["error_fixes"][0]
    assert fix["command"] == "uv run pytest" and fix["tries"] == 2 and fix["changed"] == ["cart.py"]
    assert "AssertionError" in fix["error"] and "cents" in fix["said"]
    assert p["unsolved"] == [{"command": "npm run build", "error": "npm ERR! Missing script: build", "sessions": 1}]
    assert p["tool_errors"] == {"Bash": 3, "Edit": 1}
    assert {"command": "uv run pytest", "runs": 3, "check": True} in p["commands"]
    assert p["skills"] == {"python-rules": 1} and p["subagents"] == {"Explore": 1, "general-purpose": 1}
    assert p["slash_commands"] == {"cauce:workflow": 1} and p["models"] == {"sonnet": 10}
    assert p["plan_mode"] == 1 and p["commits"] == 1 and p["prs"] == 1
    assert p["per_session"]["minutes"] is not None
    h = p["history"]
    assert h["commits"] == 6 and h["types"] == {"fix": 2, "feat": 1} and h["reverts"] == 1
    assert h["fix_share"] == 0.5 and h["agent_share"] == 0.17 and h["tags"] == 1
    assert h["branch_prefixes"] == {"feat/": 1}
    assert h["fragile_files"] == [{"file": "cart.py", "fixes": 2}]
    assert [c["subject"] for c in h["fixes"]] == ["arreglar el login", "fix: the total again", "fix(cart): round once"]
    assert p["cauce"] == {"tasks": 0, "team": "cauce"}
    missing = next(x for x in found["projects"] if not x["exists"])
    assert missing["dir"] == str(gone) and missing["history"] is None
    assert found["flows"][0]["projects"] and found["friction"]["corrections"] == 1
    assert "init" in found["have"]["cauce_skills"] or found["have"]["cauce_skills"]
    assert {w["name"] for w in found["have"]["workflows"]} >= {"bugfix", "feature"}
    text = survey.text(found)
    assert "3 sessions in 2 projects" in text and "1 corrections" in text and "uv run pytest" in text
    assert "6 commits since" in text and "/cauce:init" in text
    assert json.loads(survey.as_json(found))["read"]["sessions"] == 3


def test_one_project_only_limits_and_cauce_s_own_facts(git_repo, tmp_path, store):
    claude = tmp_path / "claude-config"
    for n in range(3):
        write(claude, "-repo", f"s{n}", [user(f"implementa el cambio {n}", git_repo)])
    other = tmp_path / "other"
    other.mkdir()
    write(claude, "-other", "o1", [user("arregla el bug", other)])
    key = survey.repo.key(git_repo)
    store.create_task("fix it", status="done", source="cauce", repo=key, kind="implement", final_cell="sonnet/high")
    found = survey.gather(store, only=key, per_project=2)
    assert [p["repo"] for p in found["projects"]] == [key]
    assert found["projects"][0]["sessions"] == 2 and found["read"]["skipped"]["over the per-project limit"] == 1
    assert found["projects"][0]["cauce"]["tasks"] == 1
    assert found["projects"][0]["cauce"]["by_kind"][0]["kind"] == "implement"
    assert survey.gather(None, limit=1)["read"]["skipped"]["older"] == 3  # the rest, past the limit
    assert survey.gather(None, only=key)["projects"][0]["cauce"] is None


def test_nothing_to_read(store, tmp_path):
    found = survey.gather(store)
    assert found["projects"] == [] and found["read"]["transcripts"] == 0
    assert "no Claude Code session found" in survey.text(found)


def test_the_person_s_skills_are_listed(tmp_path):
    skill = tmp_path / "claude-config" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\n---\n")
    (tmp_path / "claude-config" / "skills" / "loose").mkdir()
    assert survey.have()["claude_skills"] == ["deploy"]


def test_command_keys_errors_and_shapes():
    assert survey.command_key("cd /x && FOO=1 uv run pytest -q tests/") == "uv run pytest"
    assert survey.command_key("python -m pytest -x") == "python -m pytest"
    assert survey.command_key("/usr/bin/git status --short") == "git status"
    assert survey.command_key("npm test; echo done") == "echo done"
    assert survey.command_key("cd /x") == "cd"
    assert survey.command_key("") == ""
    assert survey.command_key("make lint test build ci") == "make lint test"
    assert survey.error_line("Exit code 2\nsomething\nError: no such file /a/b.py") == "Error: no such file /a/b.py"
    assert survey.error_line("Exit code 1\nplain") == "plain" and survey.error_line("") == ""
    assert survey.error_signature("Error at /a/b.py:12 sha 1a2b3c4d") == "error at <path>:# sha <hex>"
    facts = {"kinds": [], "plan_mode": 0, "checks": 0, "commits": 0, "prs": 0}
    assert survey.shape(facts) == "talk"
    long = {**facts, "kinds": ["plan", "implement", "test", "docs", "ui", "refactor", "chat", "explore"]}
    assert survey.shape(long).count("→") == survey.SHAPE_STEPS - 1


def test_a_history_that_cannot_be_read(tmp_path, git_repo):
    assert survey.history(tmp_path) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert survey.history(empty) is None  # no commit yet: git log fails
    assert survey.history(git_repo)["commits"] == 1
    assert survey._minutes("nope", "2026-01-01") is None


def test_the_command(git_repo, tmp_path, capsys, monkeypatch):
    write(tmp_path / "claude-config", "-repo", "s1", [user("implementa x", git_repo)])
    assert cli.main(["survey"]) == 0
    assert "1 sessions in 1 projects" in capsys.readouterr().out
    assert cli.main(["survey", "--json", "--repo", str(git_repo), "--days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["read"]["sessions"] == 1


def test_odd_transcripts_are_read_as_far_as_they_go(tmp_path, monkeypatch, git_repo):
    path = write(tmp_path / "claude-config", "-repo", "odd", [
        [1, 2],
        user([{"type": "text", "text": "pinta el header #ui"}], git_repo),
        assistant([tool("b1", "Bash", command="FOO=1"), tool("b2", "Bash", command="ls"), "not a block",
                   {"type": "tool_use", "id": "b3", "name": "Read", "input": "not a mapping"},
                   tool("p1", "ExitPlanMode")]),
        result("b1", "Exit code 1\nboom", error=True),
        result("b2", "a b c"),
        {"type": "assistant", "message": {"content": 7}},
        user("y" * 50, git_repo),
    ])
    facts = survey.read_session(path)
    assert facts["kinds"] == ["ui"] and facts["unsolved"] == [] and facts["commands"] == {"ls": 1}
    assert survey.shape(facts) == "plan → ui"
    monkeypatch.setattr(survey, "MAX_BYTES", 10)
    assert survey.read_session(path)["requests"] == []  # past the cap, nothing more is read
    monkeypatch.setattr(survey, "MAX_BYTES", 10**9)
    monkeypatch.setattr(survey.compact, "MAX_LINE", 10)
    assert survey.read_session(path)["requests"] == []  # every line too long to read whole


def test_nothing_left_out_says_nothing(store):
    found = survey.gather(store)
    assert "left out" not in survey.text(found)
