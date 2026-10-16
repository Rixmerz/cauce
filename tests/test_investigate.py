from __future__ import annotations

import json

import pytest

from cauce import cli, criteria, flow, insights, investigate, launch, roles
from cauce.launch import WorkerResult
from cauce.matrix import Cell
from cauce.orchestrate import Options, run

from .test_orchestrate import Script
from .test_survey import deep_repo, user, write


def session_in(tmp_path, where, sid="s1"):
    write(tmp_path / "claude-config", f"-{where.name}", sid, [user("arregla el total del carrito", where)])


def plugin_in(tmp_path, name="helper"):
    root = tmp_path / "claude-config" / "plugins" / "cache" / "market" / name / "1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}))
    (root / "skills" / "flow").mkdir(parents=True)
    (root / "skills" / "flow" / "SKILL.md").write_text("---\nname: flow\ndescription: phases and gates\n---\nx")
    return root


def test_a_plan_reads_each_project_deep_and_says_what_it_leaves_out(git_repo, tmp_path, store):
    deep_repo(git_repo)
    session_in(tmp_path, git_repo)
    gone = tmp_path / "gone"
    session_in(tmp_path, gone, "s2")
    other = tmp_path / "other"
    other.mkdir()
    session_in(tmp_path, other, "s3")
    plugin_in(tmp_path)
    planned = investigate.plan(store, exclude=[str(other)], plugin_names=["helper"])
    assert [p["dir"] for p in planned["projects"]] == [str(git_repo)]
    assert planned["projects"][0]["deep"]["history"]["chains"]
    assert sorted(x["why"] for x in planned["left_out"]) == [f"{gone} is gone: nothing to read there",
                                                            "left out by the person"]
    assert planned["plugins"][0]["name"] == "helper"
    with pytest.raises(investigate.InvestigateError, match="no installed plugin nope; installed: helper"):
        investigate.plan(store, plugin_names=["nope"])
    assert investigate.plan(store, repos=[str(git_repo)])["projects"][0]["dir"] == str(git_repo)


def test_start_queues_one_read_only_investigator_per_project_and_plugin_each_in_its_lane(git_repo, tmp_path, store):
    deep_repo(git_repo)
    session_in(tmp_path, git_repo)
    root = plugin_in(tmp_path)
    kicked = []
    started = investigate.start(store, investigate.plan(store, plugin_names=["helper"]), session_id="sess",
                                kick=lambda cwd, key: kicked.append((cwd, key)), budget=2.5)
    assert started["id"].startswith("inv-") and len(started["tasks"]) == 2 and started["cap_usd"] == 5.0
    assert kicked == [(t["cwd"], t["repo"]) for t in started["tasks"]] and kicked[1][0] == str(root)
    task = store.get_task(started["tasks"][0]["id"])
    options = json.loads(task["options"])
    assert options == {"kind": "explore", "role": "surveyor", "start": "opus/high", "budget_usd": 2.5,
                       "max_turns": investigate.MAX_TURNS, "keep_learned": False, "investigation": started["id"]}
    assert task["status"] == "queued" and task["session_id"] == "sess" and task["title"].startswith("[investigate")
    # What it must cover is listed, so each line is a criterion a pass must answer.
    assert len(criteria.items(task["body"])) == 6 and "feat(cart): totals in cents" in task["body"]
    plugin_task = store.get_task(started["tasks"][1]["id"])
    assert len(criteria.items(plugin_task["body"])) == 5 and '"workflow_step_keys"' in plugin_task["body"]
    queued = flow.queued_options(task, Options())
    assert queued.role == "surveyor" and queued.start == Cell("opus", "high") and not queued.keep_learned
    assert insights.work(store, task["repo"])["tasks"] == 0  # cauce's reading is not the person's work
    with pytest.raises(investigate.InvestigateError, match="nothing to investigate"):
        investigate.start(store, {"projects": [], "plugins": [], "left_out": []})
    with pytest.raises(ValueError):
        investigate.start(store, investigate.plan(store), start="nonsense")


def test_status_reports_each_task_its_cost_its_stop_and_its_report(git_repo, tmp_path, store):
    with pytest.raises(investigate.InvestigateError, match="no investigation yet"):
        investigate.status(store)
    session_in(tmp_path, git_repo)
    started = investigate.start(store, investigate.plan(store), kick=lambda *a: None)
    task_id = started["tasks"][0]["id"]
    found = investigate.status(store)
    assert found["id"] == started["id"] and not found["done"] and found["tasks"][0]["report"] is None
    store.update_task(task_id, status="done", cost_usd=1.234)
    store.add_event(task_id, "report", seq=1, passed=True, report={"project": "x", "families": []})
    store.add_event(task_id, "finished", status="done", stop=None)
    found = investigate.status(store, started["id"])
    assert found["done"] and found["cost_usd"] == 1.23 and found["tasks"][0]["report"] == {"project": "x",
                                                                                           "families": []}
    assert "done, $1.23" in investigate._text(found) and "[report]" in investigate._text(found)
    with pytest.raises(investigate.InvestigateError, match="no investigation inv-0"):
        investigate.status(store, "inv-0")


def test_wait_says_each_change_until_every_task_ended(git_repo, tmp_path, store):
    session_in(tmp_path, git_repo)
    started = investigate.start(store, investigate.plan(store), kick=lambda *a: None)
    task_id = started["tasks"][0]["id"]
    said = []

    def sleep(_):
        store.update_task(task_id, status="blocked")
        store.add_event(task_id, "finished", status="blocked", stop={"cause": "budget", "reason": "spent"})

    found = investigate.wait(store, None, 60, poll=0, sleep=sleep, say=said.append)
    assert found["done"] and said[0].endswith("queued — " + started["tasks"][0]["title"]) and "blocked" in said[1]
    assert found["tasks"][0]["stopped"] == "spent" and "[spent]" in investigate._text(found)
    assert investigate.wait(store, None, 0, say=said.append)["done"]


def test_the_command(git_repo, tmp_path, store, capsys, monkeypatch):
    session_in(tmp_path, git_repo)
    assert cli.main(["investigate", "start", "--dry-run"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["projects"][0]["dir"] == str(git_repo) and shown["cap_usd"] == investigate.BUDGET_USD
    assert cli.main(["investigate", "start", "--plugin", "nope"]) == 1
    assert "no installed plugin" in capsys.readouterr().err
    assert cli.main(["investigate", "status"]) == 1
    monkeypatch.setattr(investigate, "_kick", lambda *a: True)
    assert cli.main(["investigate", "start", "--start", "sonnet/high", "--budget", "3"]) == 0
    out = capsys.readouterr().out
    assert "1 investigator(s) queued" in out and "at most $3.00" in out
    other = tmp_path / "other"
    other.mkdir()
    session_in(tmp_path, other, "s9")
    assert cli.main(["investigate", "start", "--json", "--repo", str(other), "--exclude", str(other)]) == 1
    assert cli.main(["investigate", "start", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["tasks"]) == 2
    assert cli.main(["investigate", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["done"] is False
    assert cli.main(["investigate"]) == 0
    assert "running" in capsys.readouterr().out
    assert cli.main(["investigate", "status", "--wait", "0.01"]) == 0
    monkeypatch.setattr(investigate.dispatch, "held", lambda *a: False)
    monkeypatch.setattr(investigate.dispatch, "start", lambda *a: True)
    assert investigate._kick(str(git_repo), "k") is True


def test_cli_lists_what_was_left_out(git_repo, tmp_path, store, capsys, monkeypatch):
    session_in(tmp_path, git_repo)
    gone = tmp_path / "gone"
    session_in(tmp_path, gone, "s2")
    monkeypatch.setattr(investigate, "_kick", lambda *a: True)
    assert cli.main(["investigate", "start"]) == 0
    assert "left out:" in capsys.readouterr().out


# --- the report a worker returns, and the investigator that writes it ----------------------


def test_a_report_block_is_read_beside_the_result():
    text = ('```cauce-report\n{"project": "x"}\n```\n```cauce-result\n'
            '{"verdict": "pass", "summary": "read", "evidence": "git log: 3 commits"}\n```')
    assert launch.report_block(text) == {"project": "x"}
    assert launch.report_block("```cauce-report\nnot json\n```") == {"unparsed": "not json"}
    assert launch.report_block("```cauce-report\n[1]\n```") == {"unparsed": "[1]"}
    assert launch.report_block("nothing") is None


def test_an_investigator_runs_read_only_keeps_its_report_and_files_nothing(git_repo, store, monkeypatch):
    monkeypatch.setenv("CAUCE_NOTES", "on")
    script = Script(WorkerResult(True, None, "read every branch", "git log --all: 12 commits",
                                 learned=("Commits use conventional types",), report={"project": "x"}))
    report = run("Investigate this project", git_repo, store,
                 Options(kind="explore", role="surveyor", keep_learned=False), registry={}, launcher=script)
    assert report.status == "done"
    spec = script.specs[0]
    assert spec.append_system_prompt.startswith("You work as the `surveyor` role")
    assert set(launch.WRITE_TOOLS) <= set(spec.disallowed_tools)
    assert store.last_event(report.task_id, "report")["data"]["report"] == {"project": "x"}
    assert store.notes(insights.repo.key(git_repo)) == []  # what it learned waits on the person
    assert Options(keep_learned=False).stored()["keep_learned"] is False
    assert roles.internal("surveyor").kinds == ("explore",)
