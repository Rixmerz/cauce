from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest

from locks import cli, insights, roles, teams
from locks.store import Store, home

from .conftest import git

ROOT = Path(__file__).resolve().parents[1]


def seed(store: Store, checkout: Path, key: str = "github.com/o/shop") -> str:
    for i, (kind, status, cell, cost) in enumerate([
            ("ui", "done", "sonnet/high", 0.3), ("ui", "done", "sonnet/xhigh", 0.5), ("ui", "failed", None, 1.0),
            ("implement", "done", "sonnet/medium", 0.2), ("implement", "blocked", None, 0.1),
            ("debug-repro", "failed", None, 0.9), ("debug-repro", "failed", None, 0.8),
            ("debug-repro", "done", "opus/high", 1.2), ("review-routine", "done", "sonnet/medium", 0.2),
            ("implement", "queued", None, 0.0)]):
        task = store.create_task(f"task {i}", status=status, source="locks", repo=key, cwd=str(checkout), kind=kind,
                                 final_cell=cell, cost_usd=cost)
        if status != "done":
            store.add_attempt(task["id"], cell="sonnet/medium", max_turns=30, passed=0, failure="code_bug")
    return key


def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "shop"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "pyproject.toml").write_text("[project]\n")
    (d / "app.py").write_text("")
    (d / "lib.py").write_text("")
    (d / "web").mkdir()
    (d / "web" / "page.tsx").write_text("")
    (d / "web" / "app.css").write_text("")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "files")
    return d


def test_insights_read_each_project_s_work_languages_and_team(store, tmp_path):
    d = checkout(tmp_path)
    key = seed(store, d)
    teams.create("web", ["frontend", "implementer"])
    teams.use(key, "web")
    agents = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "agents"
    agents.mkdir(parents=True)
    (agents / "mine.md").write_text("---\nname: mine\ndescription: does mine\n---\nbody\n")
    (agents / "broken.md").write_text("---\nname\n---\n")
    (agents / "partial.md").write_text("---\nname: partial\n---\n")
    found = insights.gather(store)
    p = found["projects"][0]
    assert p["repo"] == key and p["exists"] and p["team"] == "web" and p["work"]["tasks"] == 10
    ui = next(r for r in p["work"]["by_kind"] if r["kind"] == "ui")
    assert ui["tasks"] == 3 and ui["pass_rate"] == 0.67 and ui["passed_at"] == {"sonnet/high": 1, "sonnet/xhigh": 1}
    debug = next(r for r in p["work"]["by_kind"] if r["kind"] == "debug-repro")
    assert debug["stopped"] == {"failed": 2} and not debug["reads"]
    assert p["work"]["failures"] == {"code_bug": 5}
    langs = {x["language"]: x["share"] for x in p["languages"]["by_share"]}
    assert langs == {"python": 0.67, "typescript": 0.33} and p["languages"]["manifests"] == ["pyproject.toml"]
    assert p["languages"]["ui_files"] == 2
    assert "user interface" in p["purpose"] and "debugging" in p["purpose"] and "python" in p["purpose"]
    joined = " | ".join(p["suggestions"])
    assert "locks roles new implementer-python --extends implementer --match pyproject.toml --skill python-rules" \
        in joined
    assert "debug-repro passes 33% here" in joined and "never asked for here" in joined
    assert found["claude_agents"] == [{"name": "mine", "description": "does mine"}]
    assert {t["name"] for t in found["teams"]} == {"locks", "web"} and "security-audit" in found["skills"]
    text = insights.text(found)
    assert f"{key} ({d}): team web, 10 tasks" in text and "python 67%" in text and "→ a python implementer" in text
    assert "your Claude Code subagents: mine" in text
    assert json.loads(insights.as_json(found))["projects"][0]["repo"] == key


def test_a_project_gone_from_disk_and_an_empty_store_still_read(store, tmp_path):
    assert "has not worked in any project yet" in insights.text(insights.gather(store))
    store.create_task("x", status="done", source="locks", repo="github.com/o/gone", cwd=str(tmp_path / "gone"),
                      kind="docs")
    p = insights.gather(store)["projects"][0]
    assert not p["exists"] and p["languages"]["files"] == 0 and p["purpose"] == ["documentation"]
    assert insights.purpose({"work": {"by_kind": []}, "languages": {"ui_files": 0, "by_share": []}}) == ["general"]
    rows = {"work": {"by_kind": [{"kind": k, "tasks": n} for k, n in
                                 [("review-routine", 3), ("docs", 3), ("feature", 4)]]},
            "languages": {"ui_files": 0, "by_share": []}}
    assert insights.purpose(rows) == ["review", "documentation", "building"]


def test_a_language_found_by_files_alone_is_matched_by_its_extension(tmp_path):
    found = {"languages": {"by_share": [{"language": "go", "share": 1.0}], "manifests": []},
             "work": {"by_kind": [], "tasks": 0}, "checks": ["edit:go > bash:go-test"]}
    joined = " | ".join(insights.suggestions(found))
    assert "--match **/*.go --skill go-rules" in joined and "`edit:go > bash:go-test`" in joined
    assert insights.languages(tmp_path)["by_share"] == []


def test_roles_new_builds_on_a_parent_and_adds_to_its_lists():
    path = roles.new("implementer-go", extends="implementer", match=["go.mod"], skills=["go-rules"],
                     disallowed_tools=["WebFetch"], capabilities=["index"], on_demand=["sql-rules"],
                     prompt="Run go test ./... first.")
    role = roles.load().get("implementer-go")
    parent = roles.load().get("implementer")
    assert role.extends == "implementer" and role.kinds == ("implement",) and role.match == ("go.mod",)
    assert role.skills == (*parent.skills, "go-rules") and role.on_demand == parent.on_demand
    assert "add_skills: [go-rules]" in path.read_text() and "kinds:" not in path.read_text()
    assert role.prompt == parent.prompt + "\n\nRun go test ./... first."
    assert role.description.endswith("(go.mod)") and role.disallowed_tools == ("WebFetch",)
    assert roles.brief(role).system_prompt.count("go-rules") == 1 + roles.brief(role).system_prompt.count(
        "go-rules`:")
    assert path.parent == home() / "roles"
    with pytest.raises(roles.RoleError, match="already a role"):
        roles.new("implementer-go", extends="implementer")
    with pytest.raises(roles.RoleError, match="already a role"):
        roles.new("reviewer", extends="implementer")
    with pytest.raises(roles.RoleError, match="no role name"):
        roles.new("Bad", extends="implementer")
    with pytest.raises(roles.RoleError, match="no kind"):
        roles.new("x", extends="implementer", kinds=["nope"])
    with pytest.raises(roles.RoleError, match="no role 'ghost'"):
        roles.new("x", extends="ghost")
    assert not (home() / "roles" / "x.md").exists()


def test_a_derived_role_follows_its_parent_s_lists_as_they_change():
    from .test_roles import write_role

    write_role(home() / "roles", "base", "Base.", description="base", kinds=["implement"], skills=["ponytail"])
    roles.new("child", extends="base", skills=["go-rules"], match=["go.mod"])
    assert roles.load().get("child").skills == ("ponytail", "go-rules")
    write_role(home() / "roles", "base", "Base.", description="base", kinds=["implement", "test"],
               skills=["ponytail", "engineering-baseline"])
    child = roles.load().get("child")
    assert child.skills == ("ponytail", "engineering-baseline", "go-rules") and child.kinds == ("implement", "test")
    write_role(home() / "roles", "odd", description="d", kinds=["implement"], add_skills=["Bad Name"])
    assert "no skill name" in roles.load().refused["odd"]


def test_a_role_that_fails_once_written_is_not_kept(monkeypatch):
    real = roles.load

    def refusing(**kw):
        catalog = real(**kw)
        catalog.refused["late"] = "refused late"
        return catalog

    monkeypatch.setattr(roles, "load", refusing)
    with pytest.raises(roles.RoleError, match="refused late"):
        roles.new("late", extends="implementer")
    assert not (home() / "roles" / "late.md").exists()


def test_the_commands(capsys, store, tmp_path, monkeypatch):
    d = checkout(tmp_path)
    seed(store, d)
    assert cli.main(["team", "insights"]) == 0
    assert "github.com/o/shop" in capsys.readouterr().out
    assert cli.main(["team", "insights", "--json", "--limit", "1"]) == 0
    assert len(json.loads(capsys.readouterr().out)["projects"]) == 1
    assert cli.main(["roles", "new", "tester-py", "--extends", "tester", "--match", "pyproject.toml",
                     "--skill", "python-rules", "--prompt", "Use pytest."]) == 0
    assert "builds on tester" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("From stdin."))
    assert cli.main(["roles", "new", "tester-js", "--extends", "tester", "--prompt", "-"]) == 0
    assert roles.load().get("tester-js").prompt.endswith("From stdin.")
    assert cli.main(["roles", "new", "tester-py", "--extends", "tester"]) == 1
    assert "already a role" in capsys.readouterr().err


# --- the skill ------------------------------------------------------------------------


def test_the_init_skill_is_a_plugin_skill_whose_commands_all_exist():
    text = (ROOT / "skills" / "init" / "SKILL.md").read_text()
    meta, body = roles.split_frontmatter(text)
    assert meta["name"] == "init" and len(meta["description"]) > 80
    assert not (ROOT / "skills" / "personal-team").exists()
    parser = cli.build_parser()
    blocks = "\n".join(re.findall(r"```bash\n(.*?)```", body, re.S)).replace("\\\n", " ")
    commands = [line.split("  #")[0].strip() for line in blocks.splitlines() if line.startswith("locks ")]
    assert len(commands) >= 20
    for line in commands:
        argv = shlex.split(line.replace("<<'TXT'", "").replace("<<'JSON'", ""))[1:]
        parser.parse_args(argv)  # every flag the skill tells a session to pass is one locks takes
    # The workflow it shows is one locks accepts as written.
    from locks import workflows

    shown = re.search(r"<<'JSON'\n(.*?)\nJSON", body, re.S).group(1)
    defn = workflows.normalize(json.loads(shown))
    assert [s["id"] for s in defn["steps"]] == ["build", "review", "refute", "fix", "recheck"]
