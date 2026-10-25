"""`cauce loop …`: goals that run in rounds (`cauce.loops`), made, started and read."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from cauce import loops, workflows


def _add(args: argparse.Namespace) -> int:
    cwd = Path(args.dir or os.getcwd()).resolve()
    inputs = {}
    for pair in args.input or ():
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"cauce loop: --input is name=value, not {pair!r}")
            return 2
        inputs[key.strip()] = value
    try:
        defn = workflows.load(args.workflow, cwd)
        for name in ("goal", "target"):
            # The loop's goal is what a workflow that asks for one is given.
            if name in defn["inputs"] and not inputs.get(name, "").strip():
                inputs[name] = args.goal
        missing = [i for i in defn["inputs"] if not inputs.get(i, "").strip()]
        if missing:
            print(f"cauce loop: {args.workflow} needs {', '.join(missing)}: give each with --input name=value")
            return 2
        loop = loops.normalize({
            "name": args.name, "goal": args.goal, "workflow": args.workflow, "cwd": str(cwd), "inputs": inputs,
            "every_s": loops.duration(args.every) if args.every else 0,
            "until": loops.until_at(args.until) if args.until else "",
            "metric": args.metric or "", "direction": "down" if args.down else "up", "target": args.target,
            "stale": args.stale, "variants": args.variant or [], "trial": args.trial, "policy": args.policy or [],
            "hours": args.hours or "", "per_hour": args.per_hour, "exclude": args.exclude or [],
            "session_id": os.environ.get("CAUCE_SESSION_ID"), "mode": "draft",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        path = loops.save(loop, new=True)
    except (loops.LoopError, workflows.WorkflowError) as exc:
        print(f"cauce loop: {exc}")
        return 2
    print(f"loop {loop.name} saved in {path}, as a draft: nothing it does leaves until `cauce loop act {loop.name}`.")
    print(f"Start it with `cauce loop start {loop.name}`; `cauce loop show {loop.name}` says how it goes.")
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    command = args.loop_command or "list"
    if command == "add":
        return _add(args)
    try:
        if command == "list":
            rows = []
            for name in loops.names():
                loop, st = loops.load(name), loops.state(name)
                rows.append({"name": name, "mode": loop.mode, "status": st.get("status", "idle"),
                             "rounds": st.get("rounds", 0), "goal": loop.goal, "why": st.get("why")})
            if args.json:
                print(json.dumps(rows, ensure_ascii=False))
            for r in [] if args.json else rows:
                print(f"{r['name']}: {r['status']} ({r['mode']}), {r['rounds']} round(s): {r['goal']}"
                      + (f" — {r['why']}" if r["why"] else ""))
            if not rows and not args.json:
                print("no loops yet: `cauce loop add <name> --goal … --workflow …`")
            return 0
        if command == "show":
            if args.json:
                print(json.dumps({"loop": asdict(loops.load(args.name)), "state": loops.state(args.name),
                                  "diary": loops.diary(args.name)}, ensure_ascii=False))
            else:
                print(loops.summary(args.name))
            return 0
        if command == "start":
            if loops.load(args.name).mode == "draft":
                print(f"loop {args.name} starts as a draft: its tools that act are refused.")
            pid = loops.launch(args.name, os.environ)
            print(f"loop {args.name} running (pid {pid}); `cauce loop show {args.name}` says how it goes.")
            return 0
        if command == "stop":
            loops.halt(args.name, os.environ)
            print(f"loop {args.name} stops at its runner's next look; a round running goes on to its end.")
            return 0
        if command in ("act", "draft"):
            loop = loops.load(args.name)
            loop.mode = command
            loops.save(loop)
            print(f"loop {args.name} is {'set to act, within its rules' if command == 'act' else 'a draft again'}"
                  + (f": hours {loop.hours or 'any'}, at most {loop.per_hour or 'any number of'} acting calls an "
                     f"hour, {len(loop.exclude)} exclusion(s)." if command == "act" else "."))
            return 0
        if command == "rm":
            loops.remove(args.name)
            print(f"loop {args.name} removed, with its diary.")
            return 0
    except loops.LoopError as exc:
        print(f"cauce loop: {exc}")
        return 2
    return 2  # pragma: no cover - argparse knows every command


def cmd_loop_runner(args: argparse.Namespace) -> int:
    did = loops.run_forever(args.name)
    print(f"cauce loop-runner {args.name}: {did}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    top = sub.add_parser("loop", help="goals that run in rounds, each a workflow run, until evidence stops them")
    lsub = top.add_subparsers(dest="loop_command")
    top.set_defaults(func=cmd_loop, loop_command="list", json=False)
    p = lsub.add_parser("list", help="every loop and where it stands")
    p.add_argument("--json", action="store_true")
    p = lsub.add_parser("add", help="a new loop, as a draft")
    p.add_argument("name")
    p.add_argument("--goal", required=True, help="what the loop is for, in the person's words")
    p.add_argument("--workflow", required=True, help="the workflow each round runs")
    p.add_argument("--dir", help="where its rounds run (default: here)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", help="an input of the workflow")
    p.add_argument("--every", help="the least time from one round's start to the next: 90, 30m, 2h")
    p.add_argument("--until", help="when its window ends: 07:00 (next occurrence, local) or an ISO time")
    p.add_argument("--metric", help="a command whose last printed number is the metric")
    p.add_argument("--down", action="store_true", help="less of the metric is better")
    p.add_argument("--target", type=float, help="the metric that ends the loop")
    p.add_argument("--stale", type=int, default=loops.STALE, help="rounds in a row without improving that stop it")
    p.add_argument("--variant", action="append", help="a strategy to try against the others (two or more)")
    p.add_argument("--trial", type=int, default=loops.TRIAL, help="measured rounds each strategy gets")
    p.add_argument("--policy", action="append", help="one of the person's rules, in their words")
    p.add_argument("--hours", help="the local hours a tool may act in, like 8-21")
    p.add_argument("--per-hour", type=int, default=0, help="acting calls an hour at most")
    p.add_argument("--exclude", action="append", help="text that refuses an acting call that names it")
    for name, text in (("show", "where a loop stands: rounds, metric, cost, what waits on the person"),
                       ("start", "run it: a detached runner starts its rounds"),
                       ("stop", "stop it after the round running"),
                       ("act", "the person's word that it may act, within its rules"),
                       ("draft", "back to a draft: nothing acts"), ("rm", "remove it, with its diary")):
        p = lsub.add_parser(name, help=text)
        p.add_argument("name")
        if name == "show":
            p.add_argument("--json", action="store_true")
    p = sub.add_parser("loop-runner", help=argparse.SUPPRESS)
    p.add_argument("name")
    p.set_defaults(func=cmd_loop_runner)

