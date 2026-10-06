"""`cauce` — the command line the orchestrating session calls, and the hooks' entry point."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from cauce import __version__, capabilities, config, flow, habits, hooks, orchestrate, project, repo, stops, usage
from cauce.adapters import default_adapters
from cauce.classify import classify
from cauce.escalate import Attempt, Failure
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
        allow_tools=tuple(args.allow or ()),
    )
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        report = orchestrate.run(_text(args.text), Path(args.repo or os.getcwd()), store, options,
                                 session_id=os.environ.get("CAUCE_SESSION_ID"))
        if getattr(report, "task_id", None):
            store.mark_reported([report.task_id], via="cli")  # its caller reads the report below
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return 0 if report.status in ("done", "dry run") else 1


#: Endings a task can be resumed from: it stopped, and was not judged wrong.
RESUMABLE = ("blocked", "needs_approval", "failed", "cancelled", "interrupted")


def cmd_resume(args: argparse.Namespace) -> int:
    """A task that stopped, run again under its own id: on the branch its work was
    kept on, from the cell it stopped at, its earlier attempts in the brief."""
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        task = store.get_task(args.id)
        if task is None or task["source"] != "cauce" or not task["cwd"]:
            print(f"no task #{args.id} that cauce ran", file=sys.stderr)
            return 1
        outgrew = task["status"] == "replan" and (stop := stops.of(store, task)) is not None and stop.cause == "turns"
        if task["status"] not in RESUMABLE and not outgrew:
            print(f"task #{args.id} is {task['status']}; only a task that stopped ({', '.join(RESUMABLE)}) resumes"
                  + ("; rewrite it as a new task" if task["status"] == "replan" else ""), file=sys.stderr)
            return 1
        if args.unattended and (refusal := _needs_a_person(store, task, args)):
            print(refusal, file=sys.stderr)
            return 3
        if args.detach:
            return _detach(args)
        options = flow.queued_options(task, orchestrate.Options(use_model_classifier=not args.no_model))
        options.kind = options.kind or task["kind"]
        options.allow_tools = tuple(dict.fromkeys([*options.allow_tools, *(args.allow or ())]))
        options.verify = args.verify or options.verify
        options.budget_usd = args.budget or options.budget_usd
        options.allow_approval = options.allow_approval or args.allow_approval
        options.isolate = options.isolate and not args.no_isolate
        if args.max_turns or outgrew:
            # A task that outgrew its turns is not wrong: it goes on with more.
            options.max_turns = args.max_turns or stops.RESUME_TURNS
        attempts = store.attempts(args.id)
        moved = store.last_event(args.id, "moved")
        waiting = moved["data"].get("next_cell") if moved and task["status"] == "needs_approval" else None
        if waiting and not options.allow_approval:
            print(f"task #{args.id} waits for {waiting}, which needs your approval: resume it with --allow-approval",
                  file=sys.stderr)
            return 1
        start = args.start or waiting or (attempts[-1]["cell"] if attempts else None)
        options.start = Cell.parse(start) if start else None
        denied, granted = store.denials(args.id), store.allowances(args.id)
        history = [Attempt(Cell.parse(a["cell"]), int(a["max_turns"] or 0), bool(a["passed"]),
                           _failure_of(a["failure"]), a["summary"] or "", tuple(denied.get(a["seq"], ())),
                           allow=tuple(granted.get(a["seq"], ())),
                           changed=tuple(json.loads(a["changed_paths"] or "[]")))
                   for a in attempts]
        store.update_task(args.id, options=json.dumps(options.stored()), dispatched=0, cancel_requested=0)
        report = orchestrate.run(task["body"], Path(task["cwd"]), store, options, task_id=args.id, history=history)
        if not args.no_report:
            # Its report is printed to whoever ran it; a detached resume's is not,
            # and reaches the session as an ending instead.
            store.mark_reported([args.id], via="cli")
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return 0 if report.status == "done" else 1


def _needs_a_person(store: Store, task: dict, args: argparse.Namespace) -> str | None:
    """Why a resume nobody approved would only stop again, or None. A model may
    continue work; it may not grant what a person must: a refused command, or a
    cell that waits for approval. A rule the person kept since with `cauce allow`
    counts as granted."""
    from cauce import allow as allow_rules
    from cauce import grants

    stop = stops.of(store, task)
    if task["status"] == "needs_approval" and not args.allow_approval:
        return (f"task #{task['id']} waits for a cell that needs the person's approval: they resume it "
                f"(`cauce resume {task['id']} --allow-approval`)")
    if stop is not None and stop.cause == "permission":
        needed = list(stop.allow or allow_rules.from_refusals(stop.denied))
        have = {*grants.granted(task["repo"]), *(args.allow or ())}
        missing = [rule for rule in needed if rule not in have]
        if missing:
            return (f"task #{task['id']} was refused {', '.join(stop.denied[:3]) or 'a command'}: only the person "
                    f"can allow {', '.join(missing)} (`{stops.next_step(task['id'], stop)}`, or keep it for this "
                    f"repository with `cauce allow {' '.join(shlex.quote(r) for r in missing)}`)")
    return None


def _detach(args: argparse.Namespace) -> int:
    """Run the same resume in a process of its own and return at once: its ending
    reaches the session that sent the task, as every queued task's does."""
    from cauce import dispatch

    argv = [sys.executable, "-m", "cauce", "resume", str(args.id), "--no-report"]
    for flag, value in (("--verify", args.verify), ("--budget", args.budget), ("--start", args.start),
                        ("--max-turns", args.max_turns)):
        if value is not None:
            argv += [flag, str(value)]
    for rule in args.allow or ():
        argv += ["--allow", rule]
    argv += [flag for flag, on in (("--allow-approval", args.allow_approval), ("--no-isolate", args.no_isolate),
                                   ("--no-model", args.no_model)) if on]
    log = home() / "work" / f"resume-{args.id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        subprocess.Popen(argv, cwd=os.getcwd(), env=dispatch.child_env(), stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    print(f"resuming #{args.id} in the background; its ending reaches the session that sent it")
    return 0


def _failure_of(value: str | None) -> Failure | None:
    try:
        return Failure(value) if value else None
    except ValueError:
        return None


def _cancel_on_sigterm():
    """`cauce cancel` sends SIGTERM. Turned into an exception, it unwinds the run:
    `subprocess.run` kills the worker, and the run's cleanup removes its worktree."""
    def handler(signum, frame):
        raise orchestrate.Cancelled

    return signal.signal(signal.SIGTERM, handler)


def _cancel_stop(via: str) -> stops.Stop:
    return stops.Stop("cancelled", f"you cancelled it {stops.CANCEL_VIA[via]} before it ran", by="you",
                      extra={"via": via})


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
        store.request_cancel(args.id, via="cli")
        if task["pid"]:
            # Gone already: the flag still stops it at its next attempt.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(task["pid"]), signal.SIGTERM)
        elif task["status"] == "queued":
            stops.record(store, args.id, _cancel_stop("cli"))
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
        if args.json:
            from cauce.ui import api

            print(json.dumps(api.task_detail(store, args.id), ensure_ascii=False, default=str))
            return 0
        print(f"#{task['id']} [{task['status']}] {task['title']}")
        print(f"repo {task['repo']}  kind {task['kind']} ({task['class_source']}: {task['class_reason']})")
        print(f"cost ${task['cost_usd']:.2f}  start {task['start_cell']}  final {task['final_cell']}")
        refused = store.denials(args.id)
        climbed = {e["data"]["seq"]: e["data"] for e in store.events_of(args.id, "moved") if "seq" in e["data"]}
        for a in store.attempts(args.id):
            verdict = "pass" if a["passed"] else (a["failure"] or "fail")
            model = f" [{a['served_model']}]" if a.get("served_model") else ""
            print(f"  attempt {a['seq']} {a['cell']}{model} ({a['turns']} turns, ${a['cost_usd']:.2f}) {verdict}"
                  + (f" → {a['move']}: {a['move_reason']}" if a["move"] else ""))
            if a["summary"]:
                print(f"    {a['summary'][:300]}")
            if refused.get(a["seq"]):
                print(f"    refused: {', '.join(refused[a['seq']])}")
            move = climbed.get(a["seq"])
            if move:
                if move.get("to_cell"):
                    print(f"    {move.get('axis')}: {move.get('from_cell')} → {move['to_cell']}")
                for line in move.get("because") or ():
                    print(f"      · {line}")
                for line in move.get("skipped") or ():
                    print(f"      · skipped {line}")
        stop = stops.view(store, task)
        if stop:
            print(f"stopped by {stop['who']}: {stop['reason']}")
            print(f"  {stop['todo']}" + (f": {stop['next']}" if stop["next"] else ""))
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


def _keys(dirs: list[str] | None) -> set[str] | None:
    return {repo.key(Path(d).resolve()) for d in dirs} if dirs else None


def cmd_memory_list(args: argparse.Namespace) -> int:
    """Problems and their fixes, as JSON or lines: everything, or only some repositories."""
    from cauce.ui import api

    store = Store.open()
    try:
        found = api.memory(store, args.query or "", limit=args.limit, repos=_keys(args.repo))
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for p in found:
        print(f"problem #{p['id']} [{p['state']}] {p['title']}  ({p['repo']})")
        for f in p["fixes"]:
            print(f"  {f['outcome']:<8} {f['description'][:200]}")
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    from cauce.ui import api

    store = Store.open()
    try:
        found = api.projects(store, enrolled_only=not args.all)
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for p in found:
        mark = ("" if p["exists"] else "  (gone)") + ("" if p["enrolled"] else "  (not enrolled: cauce init)")
        print(f"{p['dir'] or p['repo']}  {p['sessions']} session(s), last {p['last_seen']}{mark}")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from cauce.ui import api

    store = Store.open()
    try:
        found = api.sessions(store, repos=_keys(args.repo), limit=args.limit)
    finally:
        store.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False, default=str))
        return 0
    for x in found:
        print(f"{x['id']}  {x['last_seen_at']}  {x['prompts']} prompt(s)  {x['last_prompt'] or ''}\n  {x['resume']}")
    return 0


def cmd_memory_record(args: argparse.Namespace) -> int:
    key = _repo_key(args)
    store = Store.open()
    try:
        problem = args.problem_id or store.open_problem(args.problem, repo=key, symptom=args.symptom or "")
        fix = store.add_fix(problem, args.fix, args.outcome, repo=key, why=args.why or "",
                            evidence=args.evidence or "", commit_sha=args.commit)
    finally:
        store.close()
    print(f"recorded fix #{fix} on problem #{problem}")
    return 0


def cmd_memory_invalidate(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        problem = store.invalidate_fix(args.fix_id, args.why)
    finally:
        store.close()
    if problem is None:
        print(f"no fix #{args.fix_id}", file=sys.stderr)
        return 1
    print(f"fix #{args.fix_id} disproved; problem #{problem['id']} is {problem['state']}")
    return 0


def cmd_spend(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        report = usage.spend(store, days=args.days, repo=_repo_key(args))
    finally:
        store.close()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"last {args.days} day(s): ${report['total_usd']:.2f} at API rates")
    print("sessions, by model:")
    for row in report["sessions"]:
        tokens_in = row["input_tokens"] + row["cache_read"] + row["cache_write"]
        print(f"  {row['family']:<7} {row['model']:<24} {row['output_tokens']:>9} out  {tokens_in:>11} in  "
              f"${row['usd']:.2f}")
    print("workers, by cell:")
    for row in report["workers"]:
        print(f"  {row['cell']:<14} {row['attempts']:>3} attempts  {row['passed'] or 0:>3} passed  "
              f"${row['usd'] or 0:.2f}")
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
    if args.key == "model":
        return _config_model(args)
    if args.key == "mode":
        return _config_mode(args)
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


def _config_mode(args: argparse.Namespace) -> int:
    """`cauce config mode [alias|default [mode|reset]]`: the permission mode workers run in."""
    from cauce import grants
    from cauce.matrix import MODELS

    names = (*MODELS, "default")
    if args.value is not None and args.value not in names:
        print(f"unknown model {args.value!r}; known: {', '.join(names)}", file=sys.stderr)
        return 1
    if args.value is not None and args.extra is not None:
        try:
            grants.set_mode(args.value, None if args.extra == "reset" else args.extra)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    for alias in [args.value] if args.value and args.value != "default" else MODELS:
        print(f"{alias:<7} {grants.mode_for(alias)}")
    return 0


def cmd_allow(args: argparse.Namespace) -> int:
    """Rules every worker in this repository gets, kept in the person's cauce home."""
    from cauce import grants

    where = Path(args.repo or os.getcwd()).resolve()
    key = repo.key(where)
    if not key:
        print(f"{where} is no repository cauce can key rules to", file=sys.stderr)
        return 1
    try:
        rules = grants.expand(args.rules, args.preset or ())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.rm:
        kept = grants.revoke(key, rules)
    elif rules:
        kept = grants.grant(key, rules)
    else:
        kept = grants.granted(key)
    print(f"workers in {key} get: " + (", ".join(kept) if kept else "nothing beyond their mode"))
    return 0


def _config_model(args: argparse.Namespace) -> int:
    """`cauce config model [alias [id|default]]`: what each alias runs as."""
    from cauce import models
    from cauce.matrix import MODELS

    if args.value is not None and args.value not in MODELS:
        print(f"unknown model {args.value!r}; known: {', '.join(MODELS)}", file=sys.stderr)
        return 1
    if args.value is not None and args.extra is not None:
        try:
            models.pin(args.value, None if args.extra == "default" else args.extra)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    store = Store.open()
    try:
        served, lag = store.worker_models(), {x["alias"]: x for x in models.lagging(store)}
    finally:
        store.close()
    for alias in [args.value] if args.value else MODELS:
        pin = models.pinned(alias)
        line = f"{alias:<7} {'pinned to ' + pin if pin else 'follows Claude Code'}"
        if served.get(alias):
            line += f"; workers last ran {served[alias]}"
        if alias in lag:
            line += f"; your sessions use {lag[alias]['sessions']}: `{lag[alias]['command']}` pins it"
        print(line)
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
                                         "start": args.start, "allow_tools": args.allow}.items() if v is not None}
            task = store.enqueue(_text(args.text), repo=repo.key(where), cwd=str(where), options=options,
                                 author="orchestrator" if os.environ.get("CAUCE_SESSION_ID") else "person",
                                 session_id=os.environ.get("CAUCE_SESSION_ID"))
            with contextlib.suppress(OSError, project.NotAProject):
                project.enroll(where)
            from cauce import dispatch

            then = dispatch.kick(where, repo.key(where), os.environ)
            if args.json:
                print(json.dumps({**{k: task[k] for k in ("id", "title", "status", "repo", "cwd")}, "then": then},
                                 ensure_ascii=False))
            else:
                print(f"queued #{task['id']} — {task['title']}. {then}")
            return 0
        if args.queue_command == "rm":
            task = store.get_task(args.id)
            if task is None or task["status"] != "queued":
                print(f"#{args.id} is not queued", file=sys.stderr)
                return 1
            stops.record(store, args.id, _cancel_stop("queue"))
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
    from cauce import dispatch

    key = None if args.all else repo.key(Path(args.repo or os.getcwd()).resolve())
    with dispatch.hold(home(), key or "*") as mine:
        if not mine:
            print(f"a dispatcher already runs {key or 'every repository'}; it picks this up")
            return 0
        store = Store.open()
        try:
            report = flow.work(store, repo=key, max_tasks=args.max,
                               defaults=orchestrate.Options(use_model_classifier=not args.no_model))
        finally:
            store.close()
    print(report.text())
    return 0 if all(r.status == "done" for r in report.ran) else 1


def cmd_index_livespec(args: argparse.Namespace) -> int:
    """A background refresh, run detached by SessionStart under the index's lock."""
    from cauce.adapters.livespec import Livespec

    print(Livespec().refresh(Path(args.dir)))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    where = Path(args.dir or os.getcwd()).resolve()
    if not where.is_dir():
        print(f"no such directory: {where}", file=sys.stderr)
        return 1
    try:
        folder = project.enroll(where)
    except project.NotAProject as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"enrolled: {folder} — this project and its sessions now show in the UI")
    return 0


def cmd_name_session(args: argparse.Namespace) -> int:
    """Run detached by the Stop hook: Haiku names one session."""
    from cauce import dispatch, naming

    with dispatch.hold(home(), f"name:{args.id}") as mine:
        if not mine:
            return 0
        store = Store.open()
        try:
            name = naming.name_session(store, args.id, cwd=home())
        finally:
            store.close()
    print(name or "unchanged")
    return 0


# --- notes: what a project knows, by topic ------------------------------------


def _project(args: argparse.Namespace) -> tuple[str, Path]:
    where = Path(getattr(args, "repo", None) or os.getcwd()).resolve()
    key = repo.key(where)
    if key is None:
        raise SystemExit(f"cauce: {where} is not a directory")
    return key, where


def _print_notes(found: list[dict], as_json: bool) -> None:
    from cauce import notes

    if as_json:
        print(notes.to_json(found))
        return
    if not found:
        print("no notes match")
    for n in found:
        state = "" if n["state"] == "current" else f" [{n['state']}]"
        why = f"  ({n['why']})" if n.get("why") else ""
        print(f"#{n['id']} [{n['topic']}]{state} {n['title']}{why}")
        if not n["text"].startswith(n["title"].rstrip("…")):
            print(f"  {n['text']}")
        if n["state"] == "review" and n.get("state_reason"):
            print(f"  to review: {n['state_reason']}")
        if n.get("anchors"):
            print("  about: " + ", ".join(f"{a['symbol']} ({a['path']})" if a["symbol"] else a["path"]
                                          for a in n["anchors"]))
        if n.get("links"):
            print("  links: " + ", ".join(f"{x['kind']} #{x['to']}" if x["out"] else f"#{x['to']} {x['kind']} this"
                                          for x in n["links"]))


def cmd_note(args: argparse.Namespace) -> int:
    """A person keeps a fact about this project."""
    from cauce import notes

    key, where = _project(args)
    store = Store.open()
    try:
        try:
            topic = (notes.resolve_topic(store, key, args.topic) if args.topic
                     else (notes.topics_in(_text(args.text)) or ["code"])[0])
            links = []
            for spec in args.link or []:
                target, _, kind = spec.partition(":")
                links.append((int(target.lstrip("#")), kind or "depends_on"))
            note_id, new = notes.add(store, key, _text(args.text), topic=topic, title=args.title,
                                     author="session" if os.environ.get("CAUCE_SESSION_ID") else "person",
                                     filed_by="person" if args.topic else "rule",
                                     source="cauce note", session_id=os.environ.get("CAUCE_SESSION_ID"),
                                     links=links, paths=args.anchor or [], repo_dir=where)
        except ValueError as exc:
            print(f"cauce: {exc}", file=sys.stderr)
            return 2
        note = store.get_note(note_id)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"id": note_id, "new": new, "topic": note["topic"], "title": note["title"]},
                         ensure_ascii=False))
    else:
        print(f"{'kept' if new else 'already known as'} #{note_id} [{note['topic']}] {note['title']}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """The notes a question needs: by topic, by the code it is about, one link away."""
    from cauce import notes

    key, where = _project(args)
    store = Store.open()
    try:
        notes.review(store, key, where)
        try:
            in_topics = [notes.resolve_topic(store, key, t) for t in args.topic] if args.topic else None
        except ValueError as exc:
            print(f"cauce: {exc}", file=sys.stderr)
            return 2
        states = notes.STATES if args.all_states else notes.LIVE
        found = notes.recall(store, key, _text(args.text or ""), in_topics=in_topics, paths=args.path or [],
                             limit=args.limit, hops=args.hops, states=states)
    finally:
        store.close()
    _print_notes(found, args.json)
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    from cauce import notes

    key, where = _project(args)
    store = Store.open()
    try:
        command = args.notes_command or "list"
        try:
            if command == "list":
                notes.review(store, key, where)
                in_topics = [notes.resolve_topic(store, key, t) for t in args.topic] if args.topic else None
                states = ["review"] if args.review else (notes.STATES if args.all_states else notes.LIVE)
                rows = store.notes(key, topics=in_topics, states=states, limit=args.limit)
                _print_notes(notes.expand(store, [r["id"] for r in rows]), args.json)
            elif command == "show":
                found = notes.expand(store, [args.id])
                if not found or found[0]["project"] != key:
                    print(f"cauce: no note #{args.id} in this project", file=sys.stderr)
                    return 1
                _print_notes(found, args.json)
            elif command == "topics" and args.json:
                counts = store.note_counts(key)
                print(json.dumps([{"name": name, "description": about,
                                   "notes": sum(counts.get(name, {}).get(s, 0) for s in notes.LIVE)}
                                  for name, about in notes.topics(store, key).items()], ensure_ascii=False))
            elif command == "topics":
                counts = store.note_counts(key)
                for name, about in notes.topics(store, key).items():
                    by = counts.get(name, {})
                    print(f"{name:<14} {sum(by.get(s, 0) for s in notes.LIVE):>3} note(s)  {about}")
                proposed = [n for n in store.notes(key, states=notes.LIVE) if n["proposed_topic"]]
                for n in proposed:
                    print(f"  proposed topic {n['proposed_topic']!r} for #{n['id']} {n['title']} "
                          f"(add it with `cauce notes topic add {n['proposed_topic']} \"<what it holds>\"`)")
            elif command == "topic":
                name = notes.add_topic(store, key, args.name, args.description)
                print(f"topic {name} added to this project")
            else:
                note = store.get_note(args.id)
                if note is None or note["project"] != key:
                    print(f"cauce: no note #{args.id} in this project", file=sys.stderr)
                    return 1
                if command == "ok":
                    notes.confirm(store, args.id, where)
                    print(f"#{args.id} is current again, anchored to the code as it is now")
                elif command == "drop":
                    store.update_note(args.id, state="dropped", state_reason=args.why or "dropped by a person")
                    print(f"#{args.id} dropped")
                elif command == "move":
                    topic = notes.resolve_topic(store, key, args.topic)
                    store.update_note(args.id, topic=topic, proposed_topic=None)
                    print(f"#{args.id} moved to {topic}")
                elif command == "link":
                    notes.link(store, args.id, args.to, args.kind)
                    print(f"#{args.id} {args.kind} #{args.to}")
                elif command == "unlink":
                    gone = store.unlink_notes(args.id, args.to)
                    print(f"{gone} link(s) between #{args.id} and #{args.to} removed")
        except ValueError as exc:
            print(f"cauce: {exc}", file=sys.stderr)
            return 2
    finally:
        store.close()
    return 0


def cmd_extract_notes(args: argparse.Namespace) -> int:
    """Run detached by PreCompact and SessionEnd: Haiku keeps what a session established."""
    from cauce import dispatch, notes

    where = Path(args.cwd).resolve()
    key = repo.key(where)
    if key is None:
        return 0
    with dispatch.hold(home(), f"notes:{args.session}") as mine:
        if not mine:
            return 0
        store = Store.open()
        try:
            filed = notes.extract(store, key, args.session, Path(args.transcript), repo_dir=where,
                                  symbols_of=notes.livespec_symbols(where))
        finally:
            store.close()
    for f in filed:
        print(f"{'kept' if f['new'] else 'already known'} #{f['id']} [{f['topic']}] {f['title']}")
    return 0


def cmd_run_queued(args: argparse.Namespace) -> int:
    """One task the dispatcher claimed, in its own process."""
    store = Store.open()
    previous = _cancel_on_sigterm()
    try:
        task = store.get_task(args.id)
        if task is None or task["status"] != "running" or not task["dispatched"]:
            print(f"#{args.id} is not a task the dispatcher claimed", file=sys.stderr)
            return 1
        options = flow.queued_options(task, orchestrate.Options(use_model_classifier=not args.no_model))
        report = orchestrate.run(task["body"], Path(task["cwd"]), store, options, task_id=task["id"])
    finally:
        signal.signal(signal.SIGTERM, previous)
        store.close()
    print(report.text())
    return 0 if report.status == "done" else 1


def _candidates(store: Store, days: int) -> list[habits.Candidate]:
    habits.load_events(store)
    return habits.candidates(store.tool_events(days=days))


def cmd_habits(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.habits_command == "install":
            found = {c.id: c for c in _candidates(store, args.days)}
            if args.candidate not in found:
                print(f"no candidate {args.candidate} passes the gates now; see `cauce habits`", file=sys.stderr)
                return 1
            try:
                habit_id = habits.install(store, found[args.candidate], command=args.command,
                                          repo_dir=Path(args.repo or os.getcwd()).resolve())
            except habits.HabitError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"installed habit #{habit_id}: after {found[args.candidate].steps[0]}, run `{args.command}`. "
                  f"`cauce habits uninstall {habit_id}` removes it.")
            return 0
        if args.habits_command == "uninstall":
            if not habits.uninstall(store, args.habit_id):
                print(f"no habit #{args.habit_id}", file=sys.stderr)
                return 1
            print(f"removed habit #{args.habit_id}")
            return 0
        if args.habits_command == "status":
            for h in store.habits():
                state = f"off since {h['disabled_at']} after {h['failures']} failures" if h["disabled_at"] else "on"
                print(f"#{h['id']} {' → '.join(json.loads(h['steps']))}: `{h['command']}` "
                      f"({h['runs']} runs) {state}")
            return 0
        found = _candidates(store, args.days)
        if not found:
            print("no repeated sequence passes the gates yet")
        for c in found[: args.limit]:
            print(c.line())
        return 0
    finally:
        store.close()


def cmd_habit_run(args: argparse.Namespace) -> int:
    """The body of an installed habit's hook. Fails open, prints nothing."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        store = Store.open()
        try:
            habits.run_habit(store, args.habit_id, event)
        finally:
            store.close()
    except Exception:  # a hook never takes the session down, and says when it failed
        from cauce import signature

        signature.note_error(os.environ)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from cauce.ui import server

    try:
        ui = server.serve(args.port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        return _ui_port_taken(args)
    url = f"http://127.0.0.1:{ui.server_address[1]}/"
    print(f"cauce UI at {url} (Ctrl-C stops it)", flush=True)
    if args.open:
        import webbrowser

        webbrowser.open(url)
    try:
        ui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop_event.set()
        ui.server_close()
    return 0


def _ui_port_taken(args: argparse.Namespace) -> int:
    """The port is taken. By this same cauce: it is the board, say where. By an
    older one: it shows what that version showed, and it is the person's to stop."""
    from cauce.ui import server

    url = f"http://127.0.0.1:{args.port}/"
    found = server.occupant(args.port)
    if found and found["cauce"] and found["version"] == __version__:
        print(f"cauce UI {__version__} is already at {url}", flush=True)
        if args.open:
            import webbrowser

            webbrowser.open(url)
        return 0
    if found and found["cauce"]:
        print(f"port {args.port} is held by a cauce UI {found['version'] or 'from before 0.4.1'}, started before "
              f"cauce {__version__} was installed: it still serves that version's board. Stop it (end the "
              f"session or background command that started it), then run this again; or use "
              f"`cauce ui --port <another>`.", file=sys.stderr)
        return 1
    print(f"port {args.port} is taken by another program; use `cauce ui --port <another>`", file=sys.stderr)
    return 1


def cmd_link(args: argparse.Namespace) -> int:
    from cauce import link

    target_dir = Path(args.dir).expanduser() if args.dir else link.default_dir(os.environ)
    if args.remove:
        gone = link.remove(target_dir)
        print(f"removed {gone}" if gone else f"no `cauce link` shim in {target_dir}")
        return 0
    launcher = Path(__file__).resolve().parents[2] / "bin" / "cauce"
    try:
        target = link.write(target_dir, launcher)
    except FileExistsError as exc:
        print(f"cauce: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {target}: it runs the newest cauce Claude Code has installed")
    if not link.on_path(target_dir, os.environ):
        print(f"{target_dir} is not on PATH; add it to your shell profile: export PATH=\"{target_dir}:$PATH\"")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    """The board as typed JSON: a status line or a mod reads fields, never a sentence.

    `--json` prints the counts; `--full` the whole board. `--repo DIR` (repeatable)
    keeps only those repositories, and the output names the key each directory has.
    """
    from cauce.ui import api

    keys = {d: repo.key(Path(d).resolve()) for d in args.repo or []}
    store = Store.open()
    try:
        board = api.board(store, repos=set(keys.values()) if args.repo else None)
    finally:
        store.close()
    if args.full:
        print(json.dumps({**board, "repos": keys}, ensure_ascii=False, default=str))
    elif args.json:
        print(json.dumps(board["counts"]))
    else:
        c = board["counts"]
        print(f"cauce ⚠{c['needs_you']} ▶{c['running']} ⏸{c['queued']}")
    return 0


def cmd_endings(args: argparse.Namespace) -> int:
    """What ended since a session last heard: the same notice the Stop hook and the
    next prompt deliver. `--peek` reads it without taking it."""
    store = Store.open()
    try:
        text, ids = hooks.endings(store, args.session, claim=None if args.peek else args.via)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"notice": text, "ids": ids}, ensure_ascii=False))
    elif text:
        print(text)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(args.event, sys.stdin, sys.stdout, os.environ)


ALLOW_HELP = ("a permission rule the workers get, e.g. 'Bash(npm run build:*)' or 'Bash(node:*)', and "
              "'Read(//abs/path)' with two slashes for an absolute path; repeatable. A one-shot worker is "
              "refused whatever its settings do not allow; a blocked task's report prints the rules it needs")


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
        p.add_argument("--max-turns", type=int, default=30,
                       help="turns per attempt; raised once, then again while the work moves")
        p.add_argument("--verify", help="command whose exit code decides a claimed pass")
        p.add_argument("--kind", choices=KINDS, help="skip classification")
        p.add_argument("--start", help="pin the first cell, e.g. sonnet/high")
        p.add_argument("--allow-approval", action="store_true", help="allow cells that need approval (Fable)")
        p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
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

    p = sub.add_parser("resume", help="run a task that stopped again: from its kept work and the cell it stopped at")
    p.add_argument("id", type=int)
    p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
    p.add_argument("--verify", help="command whose exit code decides a claimed pass (default: the task's own)")
    p.add_argument("--budget", type=float, help="dollars this run may spend (default: the task's own)")
    p.add_argument("--start", help="the cell to resume at (default: where it stopped)")
    p.add_argument("--allow-approval", action="store_true", help="allow cells that need approval (Fable)")
    p.add_argument("--no-isolate", action="store_true",
                   help="work in the checkout (a task started with --no-isolate already resumes there)")
    p.add_argument("--max-turns", type=int,
                   help="turns per attempt; a task that outgrew its turns resumes with 120 by default")
    p.add_argument("--no-model", action="store_true")
    p.add_argument("--detach", action="store_true", help="run it in the background; its ending reaches the session")
    p.add_argument("--unattended", action="store_true",
                   help="refuse (exit 3) when only a person can clear what stopped it: a refusal, an approval")
    p.add_argument("--no-report", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_resume)

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
    p.add_argument("--allow", action="append", metavar="RULE", help=ALLOW_HELP)
    p.add_argument("--json", action="store_true", help="print the queued task as JSON")
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

    p = sub.add_parser("init", help="enroll a project: its .cauce/ folder, so it and its sessions show in the UI")
    p.add_argument("dir", nargs="?", help="a directory in the project (default: here)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index-livespec", help=argparse.SUPPRESS)
    p.add_argument("dir")
    p.set_defaults(func=cmd_index_livespec)

    p = sub.add_parser("name-session", help=argparse.SUPPRESS)
    p.add_argument("id")
    p.set_defaults(func=cmd_name_session)

    p = sub.add_parser("run-queued", help=argparse.SUPPRESS)
    p.add_argument("id", type=int)
    p.add_argument("--no-model", action="store_true")
    p.set_defaults(func=cmd_run_queued)

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
    p.add_argument("--json", action="store_true", help="everything the UI's task view shows, as JSON")
    p.set_defaults(func=cmd_show)

    mem = sub.add_parser("memory", help="problems and the fixes tried against them").add_subparsers(
        dest="memory_command", required=True)
    p = mem.add_parser("search", help="search every repository, this one first")
    p.add_argument("text")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_memory_search)
    p = mem.add_parser("list", help="problems and their fixes, latest first or matching --query")
    p.add_argument("--query")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_memory_list)
    p = mem.add_parser("record", help="record a fix and whether it worked")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--problem", help="the problem's title (opens it if new)")
    group.add_argument("--problem-id", type=int)
    p.add_argument("--fix", required=True, help="what was changed")
    p.add_argument("--outcome", required=True, choices=FIX_OUTCOMES)
    p.add_argument("--why", help="why it failed, if it did")
    p.add_argument("--evidence", help="error text or test output")
    p.add_argument("--symptom")
    p.add_argument("--commit", help="the commit the verdict is about")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_memory_record)
    p = mem.add_parser("invalidate", help="a fix believed to work, shown wrong")
    p.add_argument("fix_id", type=int)
    p.add_argument("--why", required=True)
    p.set_defaults(func=cmd_memory_invalidate)

    p = sub.add_parser("note", help="keep a fact about this project in its notes: "
                                     "cauce note \"<fact>\" --topic business|code|decisions|conventions|environment")
    p.add_argument("text", help="the fact; - reads it from stdin")
    p.add_argument("--topic", help="where it is filed (default: the topic its words point to)")
    p.add_argument("--title")
    p.add_argument("--anchor", action="append", metavar="PATH",
                   help="a file the fact is about, relative to the checkout's top (repeatable)")
    p.add_argument("--link", action="append", metavar="ID[:KIND]",
                   help="a note it relates to: depends_on (default), explains, replaces, contradicts, example_of")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("recall", help="what this project's notes say: by topic, by file, one link away")
    p.add_argument("text", nargs="?", help="the question; empty for the latest notes")
    p.add_argument("--topic", action="append", help="only this topic (repeatable)")
    p.add_argument("--path", action="append", help="notes about this file (repeatable)")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--hops", type=int, default=1, help="how many links away to follow (default 1)")
    p.add_argument("--all-states", action="store_true", help="also notes replaced or dropped")
    p.add_argument("--json", action="store_true")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_recall)

    notes_p = sub.add_parser("notes", help="this project's notes: list, show, ok, drop, move, link, topics")
    notes_p.add_argument("--repo")
    notes_p.set_defaults(func=cmd_notes, notes_command=None, topic=None, review=False, all_states=False,
                         limit=100, json=False)
    nsub = notes_p.add_subparsers(dest="notes_command")
    p = nsub.add_parser("list", help="notes, latest first")
    p.add_argument("--topic", action="append")
    p.add_argument("--review", action="store_true", help="only notes whose code changed since they were written")
    p.add_argument("--all-states", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("show", help="one note, its anchors and links")
    p.add_argument("id", type=int)
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("ok", help="you checked a note to review: it is current again")
    p.add_argument("id", type=int)
    p = nsub.add_parser("drop", help="a note that is wrong or no longer matters")
    p.add_argument("id", type=int)
    p.add_argument("--why")
    p = nsub.add_parser("move", help="file a note under another topic")
    p.add_argument("id", type=int)
    p.add_argument("topic")
    p = nsub.add_parser("link", help="link two notes: cauce notes link 12 9 explains")
    p.add_argument("id", type=int)
    p.add_argument("to", type=int)
    p.add_argument("kind", choices=["depends_on", "explains", "replaces", "contradicts", "example_of"])
    p = nsub.add_parser("unlink", help="remove the links between two notes")
    p.add_argument("id", type=int)
    p.add_argument("to", type=int)
    p = nsub.add_parser("topics", help="the topics, how many notes each, and topics Haiku proposed")
    p.add_argument("--json", action="store_true")
    p = nsub.add_parser("topic", help="add a topic to this project: cauce notes topic add api \"<what it holds>\"")
    p.add_argument("action", choices=["add"])
    p.add_argument("name")
    p.add_argument("description")

    p = sub.add_parser("extract-notes", help=argparse.SUPPRESS)
    p.add_argument("session")
    p.add_argument("transcript")
    p.add_argument("cwd")
    p.set_defaults(func=cmd_extract_notes)

    p = sub.add_parser("spend", help="tokens and dollars by model and by cell")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--repo")
    p.add_argument("--all", action="store_true", help="every repository (the default)")
    p.add_argument("--this", dest="all", action="store_false", help="only this repository")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spend, all=True)

    p = sub.add_parser("capabilities", help="the MCP servers the core hands to workers")
    p.add_argument("--example", action="store_true")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("allow", help="rules every worker in this repository gets: cauce allow 'Bash(npm:*)', "
                                     "--preset read|node|python, --rm to take back")
    p.add_argument("rules", nargs="*", metavar="RULE")
    p.add_argument("--preset", action="append", help="a named set of rules: read, node, python")
    p.add_argument("--rm", action="store_true", help="take the named rules back (all of them when none is named)")
    p.add_argument("--repo", help="the repository (default: the current directory)")
    p.set_defaults(func=cmd_allow)

    p = sub.add_parser("config", help="show or change a setting: cauce config livespec off; "
                                      "cauce config model sonnet <model id>|default; "
                                      "cauce config mode <model|default> <mode>|reset")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("extra", nargs="?", help="for `model`: the model id to pin the alias to, or `default`")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("neighbours", help="what cauce sees of the neighbours it adopts")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_neighbours)

    hb = sub.add_parser("habits", help="repeated tool sequences, and the hooks a person installs from them")
    hb.set_defaults(func=cmd_habits, habits_command="list", days=30, limit=20)
    hsub = hb.add_subparsers(dest="habits_command")
    p = hsub.add_parser("list", help="sequences that pass the gates, best first")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("install", help="run a command after the edit that starts a habit (you approve it here)")
    p.add_argument("candidate")
    p.add_argument("--command", required=True, help="what to run, e.g. \"ruff format\"")
    p.add_argument("--repo")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_habits)
    p = hsub.add_parser("uninstall", help="the kill switch")
    p.add_argument("habit_id", type=int)
    p.set_defaults(func=cmd_habits)
    hsub.add_parser("status", help="installed habits, runs and failures").set_defaults(func=cmd_habits)

    p = sub.add_parser("habit-run", help=argparse.SUPPRESS)
    p.add_argument("habit_id", type=int)
    p.set_defaults(func=cmd_habit_run)

    p = sub.add_parser("ui", help="the local web UI")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("link", help="put `cauce` on a terminal's PATH; survives plugin updates")
    p.add_argument("--dir", help="where to write it (default ~/.local/bin)")
    p.add_argument("--remove", action="store_true", help="take it away again")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("endings", help="what ended since a session last heard; delivered once, --peek to look")
    p.add_argument("--session", required=True)
    p.add_argument("--peek", action="store_true", help="read it without marking it delivered")
    p.add_argument("--via", default="mod", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_endings)

    p = sub.add_parser("board", help="needs-you, running and queued counts, for a status line")
    p.add_argument("--json", action="store_true", help="the counts as JSON")
    p.add_argument("--full", action="store_true", help="the whole board as JSON")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("projects", help="the enrolled projects (a .cauce/ folder) cauce has worked in")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="also the ones not enrolled")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("sessions", help="the Claude Code sessions cauce saw, and how to resume each")
    p.add_argument("--repo", action="append", help="only this repository (repeatable)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("hook", help="Claude Code hook entry point (reads the event on stdin)")
    p.add_argument("event")
    p.set_defaults(func=cmd_hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))

