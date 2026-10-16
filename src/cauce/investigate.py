"""`cauce investigate`: reading a person's work in depth, with cauce's own workers.

`/cauce:init` needs more than a session can read in a few minutes: every branch
of every repository, each chain of fixes opened commit by commit, every error
the person pasted matched to the change that fixed it, and the workflow of
every plugin they rely on. So the reading is work cauce runs: one task per
project, and one per plugin the person wants brought in, each a read-only
worker (`claude -p`, never a Claude Code subagent) wearing the internal
`surveyor` role. Each project is its own lane, so the workers run side by
side; each starts on a strong cell, and its brief lists what it must cover as
criteria, so a pass that skips one is no pass.

A brief carries the facts cauce read without a model (`survey`, read deep) and
asks for a `cauce-report` block: flows, failure families, problems and fixes,
notes, worker instructions, branches. The reports are kept on the tasks and
read back with `status`; nothing is filed from them: what a worker learned is
not kept (`keep_learned` off), and the session builds the team, the workflows
and the memory only once the person agrees.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from cauce import dispatch, repo, roles, survey, workflows
from cauce.matrix import KINDS, Cell
from cauce.store import Store, home

#: Where an investigator starts: deep reading wants a strong model, and it climbs from here.
START = "opus/high"
BUDGET_USD = 6.0
MAX_TURNS = 120
#: How much of the facts a brief carries.
FACTS_CHARS = 60_000
#: Task states that are no ending yet.
OPEN = ("queued", "running")

Kick = Callable[[str, str | None], object]


class InvestigateError(ValueError):
    """An investigation that cannot start or be found as asked; the message says why."""


def _facts(data: Any) -> str:
    text = json.dumps(data, indent=1, ensure_ascii=False, default=str)
    return text if len(text) <= FACTS_CHARS else text[:FACTS_CHARS] + "\n… (cut: dig the rest with git)"


def project_brief(entry: Mapping[str, Any]) -> str:
    """What one project's investigator is asked, with the facts cauce read of it."""
    facts = {k: v for k, v in entry.items() if k not in ("openers",)}
    return (
        f"Investigate, without changing any file, how the work goes in this project ({entry['repo']}): what its "
        "sessions and its history show went wrong, what fixed it, what keeps coming back, and how the work flows, "
        "so the person's one team, their workflows and their memory can be built from it.\n\n"
        "Done when:\n"
        "- every chain of fixes and reverts in the facts is explained from the commits themselves, with its cause\n"
        "- every branch not merged into the default branch is accounted for: what it holds, and whether a fix or "
        "security work on it never landed\n"
        "- every error the person reported in a session is matched to the change that fixed it, or marked unsolved\n"
        "- the corrections the person made are turned into the worker instructions that would stop them\n"
        "- the flows the work follows are named with their steps, the exact commands that check them and how "
        "they land\n"
        "- every problem with its fix and outcome, every fix that did not hold and every durable fact is in the "
        "report with its evidence (a commit, a file, a session)\n\n"
        "The facts cauce read without a model, from the sessions kept on this machine and from git; start from "
        "them, then dig past them with git (every branch, `git show` of each commit you cite):\n\n"
        f"```json\n{_facts(facts)}\n```"
    )


def plugin_brief(plugin: Mapping[str, Any]) -> str:
    """What one plugin's investigator is asked: its way of working, in cauce's terms."""
    catalog = roles.load()
    shape = {
        "plugin": plugin,
        "cauce_kinds": list(KINDS),
        "cauce_roles": [{"name": r.name, "kinds": list(r.kinds), "description": r.description}
                        for r in catalog.roles.values()],
        "workflow_step_keys": sorted(workflows.STEP_KEYS),
        "workflow_example": workflows.load("feature", None),
    }
    return (
        f"Read, without changing any file, the Claude Code plugin in this folder ({plugin['name']} "
        f"{plugin['version']}) and write its way of working in cauce's terms, so the person's cauce team and "
        "workflows follow it.\n\n"
        "Done when:\n"
        "- every skill, command and agent it ships is read in full\n"
        "- its phases and the gate between each are listed, with the exact checks, commands and thresholds\n"
        "- each workflow it describes is written as a cauce workflow definition whose prompts list measurable "
        "criteria\n"
        "- each persona or kind of worker it describes is written as a cauce role built on a bundled one, "
        "carrying the plugin's skills as plugin:skill\n"
        "- what applies only to some projects says how to tell them by the files a project holds\n\n"
        f"What cauce takes (its kinds, its bundled roles, a workflow's shape):\n\n```json\n{_facts(shape)}\n```"
    )


def _options(investigation: str, start: str, budget: float, max_turns: int) -> dict[str, Any]:
    return {"kind": "explore", "role": "surveyor", "start": start, "budget_usd": budget,
            "max_turns": max_turns, "keep_learned": False, "investigation": investigation}


def _kick(cwd: str, key: str | None) -> bool:
    """Start the lane's dispatcher: an investigation is the person asking for its work,
    so it does not wait for the `autowork` setting."""
    root = home()
    return dispatch.held(root, key or "*") or dispatch.start(Path(cwd), root, key or "*")


def plan(store: Store | None, *, repos: Iterable[str] = (), exclude: Iterable[str] = (),
         plugin_names: Iterable[str] = (), days: int = 365, env: Mapping[str, str] | None = None
         ) -> dict[str, Any]:
    """What an investigation would read: the projects (with their deep facts), the plugins,
    and the projects left out with why. Reads only."""
    wanted = {k for k in (repo.key(Path(r).resolve()) for r in repos) if k}
    unwanted = {k for k in (repo.key(Path(r).resolve()) or str(Path(r).resolve()) for r in exclude) if k}
    found = survey.gather(store, days=days, limit=100_000, per_project=1000, only=wanted or None, deep=True,
                          env=env)
    projects, left = [], []
    for p in found["projects"]:
        if p["repo"] in unwanted:
            left.append({"repo": p["repo"], "why": "left out by the person"})
        elif not Path(p["dir"]).is_dir():
            left.append({"repo": p["repo"], "why": f"{p['dir']} is gone: nothing to read there"})
        else:
            projects.append(p)
    installed = {p["name"]: p for p in survey.plugins(env)}
    names = list(dict.fromkeys(plugin_names))
    unknown = [n for n in names if n not in installed]
    if unknown:
        raise InvestigateError(f"no installed plugin {', '.join(unknown)}; installed: "
                               f"{', '.join(sorted(installed)) or 'none'}")
    return {"projects": projects, "plugins": [installed[n] for n in names], "left_out": left}


def start(store: Store, planned: Mapping[str, Any], *, session_id: str | None = None, start: str = START,
          budget: float = BUDGET_USD, max_turns: int = MAX_TURNS, kick: Kick | None = None) -> dict[str, Any]:
    """Queue one investigator per project and per plugin, each in its own lane, and start the lanes."""
    Cell.parse(start)
    if not planned["projects"] and not planned["plugins"]:
        raise InvestigateError("nothing to investigate: no project with a checkout, and no plugin named")
    investigation = f"inv-{int(time.time())}"
    queued = []
    jobs = [(p["dir"], p["repo"], project_brief(p), p["repo"]) for p in planned["projects"]]
    jobs += [(p["path"], repo.key(Path(p["path"])), plugin_brief(p), f"plugin {p['name']}")
             for p in planned["plugins"]]
    for cwd, key, body, what in jobs:
        task = store.create_task(body, status="queued", source="queue", author="init", repo=key, cwd=cwd,
                                 session_id=session_id, title=f"[investigate {investigation}] {what}"[:200],
                                 options=json.dumps(_options(investigation, start, budget, max_turns)))
        queued.append({"id": task["id"], "title": task["title"], "repo": key, "cwd": cwd})
        (kick or _kick)(cwd, key)
    return {"id": investigation, "tasks": queued, "left_out": list(planned["left_out"]),
            "cap_usd": round(budget * len(queued), 2)}


def _of(task: Mapping[str, Any]) -> str | None:
    try:
        return json.loads(task.get("options") or "{}").get("investigation")
    except ValueError:
        return None


def status(store: Store, investigation: str | None = None) -> dict[str, Any]:
    """Where an investigation is (the latest when none is named): each task, its state,
    its cost, why it stopped, and its report once it has one."""
    mine = [t for t in store.list_tasks(source=("queue",), limit=2000) if _of(t)]
    if not mine:
        raise InvestigateError("no investigation yet: `cauce investigate start`")
    investigation = investigation or _of(mine[0])
    tasks = sorted((t for t in mine if _of(t) == investigation), key=lambda t: t["id"])
    if not tasks:
        raise InvestigateError(f"no investigation {investigation}")
    out = []
    for t in tasks:
        report = store.last_event(t["id"], "report")
        ended = store.last_event(t["id"], "finished")
        stop = ((ended or {}).get("data") or {}).get("stop") or {}
        out.append({"id": t["id"], "title": t["title"], "repo": t["repo"], "status": t["status"],
                    "cost_usd": round(float(t.get("cost_usd") or 0), 2), "stopped": stop.get("reason"),
                    "report": (report or {}).get("data", {}).get("report")})
    return {"id": investigation, "done": all(t["status"] not in OPEN for t in out),
            "cost_usd": round(sum(t["cost_usd"] for t in out), 2), "tasks": out}


def wait(store: Store, investigation: str | None, seconds: float, *, poll: float = 15.0,
         sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """`status`, once every task ended or `seconds` passed; each change of state said as it happens."""
    say = say or (lambda line: print(line, file=sys.stderr, flush=True))
    deadline = time.monotonic() + seconds
    seen: dict[int, str] = {}
    while True:
        found = status(store, investigation)
        for t in found["tasks"]:
            if seen.get(t["id"]) != t["status"]:
                seen[t["id"]] = t["status"]
                say(f"cauce: #{t['id']} {t['status']} — {t['title']}")
        if found["done"] or time.monotonic() >= deadline:
            return found
        sleep(poll)


def _text(found: Mapping[str, Any]) -> str:
    lines = [f"{found['id']}: {'done' if found['done'] else 'running'}, ${found['cost_usd']:.2f} so far"]
    for t in found["tasks"]:
        mark = "report" if t["report"] else (t["stopped"] or "no report yet")
        lines.append(f"  #{t['id']} {t['status']:<14} ${t['cost_usd']:.2f}  {t['title']}  [{mark}]")
    if found["done"]:
        lines.append("`cauce investigate status --json` prints every report")
    return "\n".join(lines)


# --- the command -----------------------------------------------------------------------


def cmd(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if (args.investigate_command or "status") == "status":
            found = (wait(store, args.id, args.wait) if args.wait else status(store, args.id))
            print(json.dumps(found, indent=2, ensure_ascii=False, default=str) if args.json else _text(found))
            return 0
        planned = plan(store, repos=args.repo or (), exclude=args.exclude or (), plugin_names=args.plugin or (),
                       days=args.days)
        if args.dry_run:
            shown = {"projects": [{"repo": p["repo"], "dir": p["dir"], "sessions": p["sessions"]}
                                  for p in planned["projects"]],
                     "plugins": [{"name": p["name"], "version": p["version"]} for p in planned["plugins"]],
                     "left_out": planned["left_out"], "start": args.start,
                     "cap_usd": round(args.budget * (len(planned["projects"]) + len(planned["plugins"])), 2)}
            print(json.dumps(shown, indent=2, ensure_ascii=False))
            return 0
        started = start(store, planned, session_id=os.environ.get("CAUCE_SESSION_ID"), start=args.start,
                        budget=args.budget, max_turns=args.max_turns)
    except (InvestigateError, ValueError) as exc:
        print(f"cauce: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if args.json:
        print(json.dumps(started, indent=2, ensure_ascii=False))
    else:
        print(f"{started['id']}: {len(started['tasks'])} investigator(s) queued, each in its own lane, at "
              f"{args.start}; at most ${started['cap_usd']:.2f} in all")
        for t in started["tasks"]:
            print(f"  #{t['id']} {t['title']}")
        for left in started["left_out"]:
            print(f"  left out: {left['repo']} ({left['why']})")
        print(f"`cauce investigate status {started['id']} --wait 540` waits for them and says each ending")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    top = sub.add_parser("investigate", help="read your projects and plugins in depth with cauce's own read-only "
                                             "workers, one per project, side by side; /cauce:init builds on it")
    top.set_defaults(func=cmd, investigate_command="status", id=None, wait=0, json=False)
    isub = top.add_subparsers(dest="investigate_command")
    p = isub.add_parser("start", help="queue an investigator per project (and per --plugin) and start them")
    p.add_argument("--repo", action="append", help="only this project (repeatable; default: every one with sessions)")
    p.add_argument("--exclude", action="append", help="leave this project out (repeatable)")
    p.add_argument("--plugin", action="append", help="an installed plugin to bring into cauce's terms (repeatable)")
    p.add_argument("--days", type=int, default=365, help="sessions of the last N days (default 365)")
    p.add_argument("--start", default=START, help=f"the cell each investigator starts at (default {START})")
    p.add_argument("--budget", type=float, default=BUDGET_USD, help=f"dollars each may spend (default {BUDGET_USD})")
    p.add_argument("--max-turns", type=int, default=MAX_TURNS, help=f"turns per attempt (default {MAX_TURNS})")
    p.add_argument("--dry-run", action="store_true", help="what it would read and the most it could cost; queues "
                                                          "nothing")
    p.add_argument("--json", action="store_true")
    p = isub.add_parser("status", help="where an investigation is, and its reports (the latest by default)")
    p.add_argument("id", nargs="?")
    p.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                   help="wait up to SECONDS for every investigator to end, saying each ending")
    p.add_argument("--json", action="store_true", help="every task with its full report")
