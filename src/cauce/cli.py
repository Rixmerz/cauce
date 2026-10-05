"""`cauce` — the command line the orchestrating session calls, and the hooks' entry point."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from cauce import __version__, capabilities, config, flow, hooks, orchestrate, repo
from cauce.adapters import default_adapters
from cauce.classify import classify
from cauce.matrix import KINDS, LADDERS, Cell
from cauce.store import FIX_OUTCOMES, Store, home


def _text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _repo_key(args: argparse.Namespace) -> str | None:
    if getattr(args, "all", False):
        return None
    return repo.key(Path(args.repo or os.getcwd()))


def cmd_run(args: argparse.Namespace) -> int:
    options = orchestrate.Options(
        budget_usd=args.budget,
        max_attempts=args.max_attempts,
        allow_approval=args.allow_approval,
        isolate=not args.no_isolate,
        verify=args.verify,
        launch_dir=Path(args.launch_dir) if args.launch_dir else None,
        max_turns=args.max_turns,
        use_model_classifier=not args.no_model,
        kind=args.kind,
        start=Cell.parse(args.start) if args.start else None,
        dry_run=args.dry_run,
        livespec=args.livespec,
    )
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        report = orchestrate.run(_text(args.text), Path(args.repo or os.getcwd()), store, options)
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return 0 if report.status in ("done", "dry run") else 1


def _cancel_on_sigterm():
    """`cauce cancel` sends SIGTERM. Turned into an exception, it unwinds the run:
    `subprocess.run` kills the worker, and the run's cleanup removes its worktree."""
    def handler(signum, frame):
        raise orchestrate.Cancelled

    return signal.signal(signal.SIGTERM, handler)


def cmd_cancel(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        task = store.get_task(args.id)
        if task is None:
            print(f"no task #{args.id}", file=sys.stderr)
            return 1
        if task["status"] not in ("running", "queued"):
            print(f"task #{args.id} is {task['status']}; nothing to cancel")
            return 0
        store.request_cancel(args.id)
        if task["pid"]:
            # Gone already: the flag still stops it at its next attempt.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(task["pid"]), signal.SIGTERM)
        elif task["status"] == "queued":
            store.update_task(args.id, status="cancelled")
    finally:
        store.close()
    print(f"cancel requested for task #{args.id}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = Store.open()
    after = args.after
    try:
        while True:
            for e in store.events(after=after, task_id=args.task):
                after = e["id"]
                print(json.dumps(e, ensure_ascii=False) if args.json else
                      f"{e['ts']} #{e['task_id']} {e['kind']} {json.dumps(e['data'], ensure_ascii=False)}")
            if not args.follow:
                return 0
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


def cmd_route(args: argparse.Namespace) -> int:
    args.dry_run = True
    return cmd_run(args)


def cmd_classify(args: argparse.Namespace) -> int:
    result = classify(_text(args.text), use_model=not args.no_model, cwd=home())
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_matrix(_: argparse.Namespace) -> int:
    for kind, ladder in LADDERS.items():
        print(f"{kind:16} {' → '.join(c.label for c in ladder)}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        tasks = store.list_tasks(repo=_repo_key(args), status=args.status or None, limit=args.limit)
    finally:
        store.close()
    for t in tasks:
        cell = t["final_cell"] or t["start_cell"] or ""
        print(f"#{t['id']:<5} {t['status']:<14} {t['kind'] or '-':<16} {cell:<14} {t['title']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        task = store.get_task(args.id)
        if task is None:
            print(f"no task #{args.id}", file=sys.stderr)
            return 1
        print(f"#{task['id']} [{task['status']}] {task['title']}")
        print(f"repo {task['repo']}  kind {task['kind']} ({task['class_source']}: {task['class_reason']})")
        print(f"cost ${task['cost_usd']:.2f}  start {task['start_cell']}  final {task['final_cell']}")
        for a in store.attempts(args.id):
            verdict = "pass" if a["passed"] else (a["failure"] or "fail")
            print(f"  attempt {a['seq']} {a['cell']} ({a['turns']} turns, ${a['cost_usd']:.2f}) {verdict}"
                  + (f" → {a['move']}: {a['move_reason']}" if a["move"] else ""))
            if a["summary"]:
                print(f"    {a['summary'][:300]}")
        for m in store.messages(args.id):
            print(f"--- {m['role']} {m['ts']}\n{m['text'][:2000]}")
    finally:
        store.close()
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        found = store.search(_text(args.text), repo=_repo_key(args), limit=args.limit)
    finally:
        store.close()
    if not found:
        print("nothing recorded matches")
    for p in found:
        print(f"problem #{p['id']} [{p['state']}] {p['title']}  ({p['repo']})")
        for f in p["fixes"]:
            print(f"  {f['outcome']:<8} {f['description'][:200]}" + (f" — {f['why'][:120]}" if f["why"] else ""))
    return 0


def cmd_memory_record(args: argparse.Namespace) -> int:
    key = _repo_key(args)
    store = Store.open()
    try:
        problem = args.problem_id or store.open_problem(args.problem, repo=key, symptom=args.symptom or "")
        fix = store.add_fix(problem, args.fix, args.outcome, repo=key, why=args.why or "",
                            evidence=args.evidence or "")
    finally:
        store.close()
    print(f"recorded fix #{fix} on problem #{problem}")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    if args.example:
        print(json.dumps(capabilities.EXAMPLE, indent=2))
        return 0
    path = home() / "capabilities.json"
    registry = capabilities.load(path)
    if not registry:
        print(f"no capabilities registered in {path} (see `cauce capabilities --example`)")
    for c in registry.values():
        when = c.when if c.when == "always" else ", ".join(c.when)
        extra = f"; after a failed {', '.join(c.after_failure)}" if c.after_failure else ""
        print(f"{c.name:<20} {when}{extra}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.key is None:
        for key, value in sorted(config.load().items()):
            print(f"{key} = {json.dumps(value)}")
        return 0
    if args.key not in config.DEFAULTS:
        print(f"unknown setting {args.key!r}; known: {', '.join(config.DEFAULTS)}", file=sys.stderr)
        return 1
    if args.value is None:
        print(json.dumps(config.load()[args.key]))
        return 0
    value = config._bool(args.value)
    if value is None:
        print("expected on or off", file=sys.stderr)
        return 1
    config.save({args.key: value})
    print(f"{args.key} = {json.dumps(value)}")
    return 0


def cmd_neighbours(args: argparse.Namespace) -> int:
    where = Path(args.repo or os.getcwd())
    on = config.enabled("livespec", os.environ)
    print(f"livespec setting: {'on' if on else 'off'}")
    for adapter in default_adapters():
        status = adapter.inspect(where)
        print(status.line())
        server = adapter.server()
        print(f"  runs as: {' '.join([server['command'], *server.get('args', [])]) if server else 'nothing found'}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.queue_command == "add":
            where = Path(args.repo or os.getcwd()).resolve()
            options = {k: v for k, v in {"budget_usd": args.budget, "verify": args.verify, "kind": args.kind,
                                         "start": args.start}.items() if v is not None}
            task = store.enqueue(_text(args.text), repo=repo.key(where), cwd=str(where), options=options)
            print(f"queued #{task['id']} — {task['title']}")
            return 0
        if args.queue_command == "rm":
            task = store.get_task(args.id)
            if task is None or task["status"] != "queued":
                print(f"#{args.id} is not queued", file=sys.stderr)
                return 1
            store.update_task(args.id, status="cancelled")
            print(f"removed #{args.id}")
            return 0
        for t in store.list_tasks(repo=_repo_key(args), status=["queued"], limit=200)[::-1]:
            print(f"#{t['id']:<5} {t['repo'] or '-'}  {t['title']}")
        return 0
    finally:
        store.close()


def cmd_lanes(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.unpause is not None:
            key = repo.key(Path(args.unpause).resolve()) if Path(args.unpause).is_dir() else args.unpause
            store.unpause_lane(key)
            print(f"unpaused {key}")
            return 0
        for lane in store.lanes():
            state = f"paused: {lane['reason']}" if lane["paused"] else "open"
            print(f"{lane['repo'] or '-'}  {lane['queued']} queued  {state}")
        return 0
    finally:
        store.close()


def cmd_work(args: argparse.Namespace) -> int:
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        key = None if args.all else repo.key(Path(args.repo or os.getcwd()).resolve())
        report = flow.work(store, repo=key, max_tasks=args.max,
                           defaults=orchestrate.Options(use_model_classifier=not args.no_model))
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return 0 if all(r.status == "done" for r in report.ran) else 1


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(args.event, sys.stdin, sys.stdout, os.environ)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cauce", description="route work to the right model, effort and tools")
    parser.add_argument("--version", action="version", version=f"cauce {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("text", help="the task; - reads it from stdin")
        p.add_argument("--repo", help="the repository the work is in (default: the current directory)")
        p.add_argument("--launch-dir", help="start the worker here, and let it work in the repository")
        p.add_argument("--budget", type=float, default=5.0, help="dollars this task may spend (default 5)")
        p.add_argument("--max-attempts", type=int, default=6)
        p.add_argument("--max-turns", type=int, default=30, help="turns per attempt before it is raised once")
        p.add_argument("--verify", help="command whose exit code decides a claimed pass")
        p.add_argument("--kind", choices=KINDS, help="skip classification")
        p.add_argument("--start", help="pin the first cell, e.g. sonnet/high")
        p.add_argument("--allow-approval", action="store_true", help="allow cells that need approval (Fable)")
        p.add_argument("--no-isolate", action="store_true", help="work in the checkout, not a worktree")
        p.add_argument("--no-model", action="store_true", help="classify with the rules only")
        switch = p.add_mutually_exclusive_group()
        switch.add_argument("--livespec", dest="livespec", action="store_true", default=None,
                            help="use livespec for this task even if the setting is off")
        switch.add_argument("--no-livespec", dest="livespec", action="store_false",
                            help="leave livespec out of this task")

    p = sub.add_parser("run", help="run a task: classify, attempt, escalate, remember")
    run_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("route", help="show where a task would start and how it would climb")
    run_args(p)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("classify", help="the kind of work a request is")
    p.add_argument("text")
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_classify)

    sub.add_parser("matrix", help="every kind and its ladder").set_defaults(func=cmd_matrix)

    p = sub.add_parser("tasks", help="recent tasks of this repository")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every repository")
    p.add_argument("--status", action="append")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_tasks)

    queue = sub.add_parser("queue", help="tasks waiting for a worker; `++ task` in a session queues too")
    qsub = queue.add_subparsers(dest="queue_command")
    queue.set_defaults(func=cmd_queue, queue_command="list", repo=None, all=False)
    p = qsub.add_parser("add", help="queue a task for this repository's lane")
    p.add_argument("text")
    p.add_argument("--repo")
    p.add_argument("--budget", type=float)
    p.add_argument("--verify")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--start")
    p.set_defaults(func=cmd_queue)
    p = qsub.add_parser("list", help="what is waiting (this repository; --all for every one)")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_queue)
    p = qsub.add_parser("rm", help="take a task off the queue")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("lanes", help="one serial lane per repository; a failed task pauses it")
    p.add_argument("--unpause", metavar="REPO", help="a repository path or key")
    p.set_defaults(func=cmd_lanes)

    p = sub.add_parser("work", help="run queued tasks in workers, lane by lane")
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every repository's lane")
    p.add_argument("--max", type=int, default=flow.DEFAULT_MAX_TASKS, help="stop after this many tasks")
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_work)

    p = sub.add_parser("cancel", help="stop a running or queued task")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("events", help="what runs are doing, as they do it")
    p.add_argument("--task", type=int)
    p.add_argument("--after", type=int, default=0, help="only events after this id")
    p.add_argument("--follow", "-f", action="store_true", help="keep printing new events")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("show", help="one task: its attempts and its messages")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    mem = sub.add_parser("memory", help="problems and the fixes tried against them").add_subparsers(
        dest="memory_command", required=True)
    p = mem.add_parser("search", help="search every repository, this one first")
    p.add_argument("text")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_memory_search)
    p = mem.add_parser("record", help="record a fix and whether it worked")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--problem", help="the problem's title (opens it if new)")
    group.add_argument("--problem-id", type=int)
    p.add_argument("--fix", required=True, help="what was changed")
    p.add_argument("--outcome", required=True, choices=FIX_OUTCOMES)
    p.add_argument("--why", help="why it failed, if it did")
    p.add_argument("--evidence", help="error text or test output")
    p.add_argument("--symptom")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_memory_record)

    p = sub.add_parser("capabilities", help="the MCP servers the core hands to workers")
    p.add_argument("--example", action="store_true")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("config", help="show or change a setting: cauce config livespec off")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("neighbours", help="what cauce sees of the neighbours it adopts")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_neighbours)

    p = sub.add_parser("hook", help="Claude Code hook entry point (reads the event on stdin)")
    p.add_argument("event")
    p.set_defaults(func=cmd_hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))

