"""`cauce workflow …`: make, change, keep and run workflows, and see where a run is.

Kept apart from `cli.py`; `cli.build_parser` adds it. Every command that
changes a definition checks it before writing, and prints where it went.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from cauce import project, workflows
from cauce.store import Store

STEP_FLAGS = (("title", str), ("prompt", str), ("kind", str), ("verify", str), ("start", str),
              ("budget_usd", float), ("max_turns", int))


def _cwd(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", None) or os.getcwd()).resolve()


def _step_fields(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, _ in STEP_FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            out[name] = sys.stdin.read() if value == "-" else value
    if getattr(args, "needs", None) is not None:
        out["needs"] = [n for n in args.needs.split(",") if n.strip()]
    if getattr(args, "memory", None) is not None:
        out["memory"] = [m.strip() for m in args.memory.split(",") if m.strip()] or None
    return out


def _say_saved(where: str, path: Path) -> None:
    print(f"saved in the {where} scope: {path}")


def _print_definition(defn: dict[str, Any]) -> None:
    print(f"{defn['name']} ({defn['scope']}: {defn['path']})")
    if defn.get("description"):
        print(f"  {defn['description']}")
    print(f"  inputs: {', '.join(defn['inputs']) or 'none'}")
    for step in defn["steps"]:
        after = f" after {', '.join(step['needs'])}" if step["needs"] else " (first)"
        extra = ", ".join(f"{k} {step[k]}" for k in ("kind", "start", "verify", "budget_usd", "max_turns")
                          if step.get(k) is not None)
        print(f"\n  {step['id']} — {step['title']}{after}" + (f" [{extra}]" if extra else ""))
        for line in step["prompt"].splitlines():
            print(f"      {line}")
        if step.get("memory"):
            print(f"      + memory: {', '.join(step['memory'])}")


def _print_run(run: dict[str, Any]) -> None:
    mark = {"done": "✓", "running": "▶", "queued": "…", "pending": "·"}
    print(f"run #{run['id']} {run['name']} — {run['state']} · ${run['cost_usd']:.2f} · {run['cwd']}")
    if run["inputs"]:
        print("  " + "; ".join(f"{k}: {v}" for k, v in run["inputs"].items()))
    for step in run["steps"]:
        sign = mark.get(step["state"], "!")
        task = f" #{step['task']}" if step.get("task") else ""
        cell = f" at {step['cell']}" if step.get("cell") else ""
        tries = f", try {step['tries']}" if step.get("tries", 1) > 1 else ""
        print(f"  {sign} {step['id']:<14} {step['state']}{task}{cell}{tries}")
    if run["now"]:
        print(f"  now: {', '.join(run['now'])}")
    if run["waiting"]:
        print(f"  waits on you: {', '.join(run['waiting'])} — `cauce show <task>`, then `cauce resume <task>` "
              f"or `cauce workflow retry {run['id']} <step>`")
    if run["branch"]:
        print(f"  work so far: branch {run['branch']}")


def cmd_workflow(args: argparse.Namespace) -> int:
    what = args.workflow_command
    try:
        if what == "list":
            found = workflows.listing(_cwd(args))
            if args.json:
                print(json.dumps(found, ensure_ascii=False))
                return 0
            for w in found:
                hidden = " (hidden by a nearer one)" if w["shadowed"] else ""
                body = w.get("error") or f"{' → '.join(w['steps'])} — {w.get('description') or ''}"
                print(f"{w['name']:<18} {w['scope']:<8} {body}{hidden}")
            return 0
        if what == "show":
            defn = workflows.load(args.name, _cwd(args), args.scope)
            if args.json:
                print(json.dumps(defn, ensure_ascii=False, indent=2))
            else:
                _print_definition(defn)
            return 0
        if what == "new":
            if args.copy_from:
                path = workflows.copy_to(args.copy_from, args.name, _cwd(args), scope=args.scope)
            else:
                path = workflows.save(workflows.skeleton(args.name, args.description or ""), _cwd(args), args.scope)
            _say_saved(args.scope, path)
            print(f"next: `cauce workflow step add {args.name} …`, `cauce workflow edit {args.name}`")
            return 0
        if what == "copy":
            _say_saved(args.scope, workflows.copy_to(args.source, args.target, _cwd(args), scope=args.scope,
                                                     from_scope=args.from_scope))
            return 0
        if what == "save":
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            try:
                defn = json.loads(raw)
            except ValueError as exc:
                raise workflows.WorkflowError(f"not JSON: {exc}") from exc
            if args.name:
                defn["name"] = args.name
            _say_saved(args.scope, workflows.save(defn, _cwd(args), args.scope, replace=args.replace))
            return 0
        if what == "edit":
            return _edit_in_editor(args)
        if what == "rm":
            print(f"removed {workflows.remove(args.name, _cwd(args), args.scope)}")
            return 0
        if what == "validate":
            if args.file:
                defn = workflows.normalize(json.loads(Path(args.file).read_text(encoding="utf-8")))
            else:
                defn = workflows.load(args.name, _cwd(args), args.scope)
            print(f"{defn['name']}: {len(defn['steps'])} steps, valid")
            return 0
        if what == "step":
            return _step(args)
        if what == "run":
            return _run(args)
        if what == "status":
            return _status(args)
        store = Store.open()
        try:
            if what == "cancel":
                stopped = workflows.cancel(store, args.run)
                print(f"run #{args.run} cancelled" + (f"; stopping {', '.join(f'#{t}' for t in stopped)}"
                                                      if stopped else ""))
                return 0
            if what == "retry":
                task = workflows.retry(store, args.run, args.step)
                print(f"run #{args.run}: step {args.step} queued again as #{task['id']}")
                return 0
        finally:
            store.close()
    except (workflows.WorkflowError, OSError, ValueError) as exc:
        print(f"cauce workflow: {exc}", file=sys.stderr)
        return 1
    return 2


def _step(args: argparse.Namespace) -> int:
    fields = _step_fields(args)
    if args.step_command == "add":
        step = {"id": args.step_id, **fields}

        def change(defn: dict[str, Any]) -> None:
            workflows.add_step(defn, step, after=args.after)
    elif args.step_command == "set":
        for key in args.clear or ():
            fields[key] = None
        if not fields:
            raise workflows.WorkflowError("nothing to change: pass --prompt, --kind, --needs, --clear KEY…")

        def change(defn: dict[str, Any]) -> None:
            workflows.set_step(defn, args.step_id, fields)
    else:
        def change(defn: dict[str, Any]) -> None:
            workflows.remove_step(defn, args.step_id)
    where, path = workflows.edit(args.name, _cwd(args), change, args.scope)
    _say_saved(where, path)
    return 0


def _edit_in_editor(args: argparse.Namespace) -> int:
    """The definition in $EDITOR; checked when the editor closes, and kept only if it holds."""
    defn = workflows.load(args.name, _cwd(args), args.scope)
    where = defn["scope"] if defn["scope"] != "bundled" else workflows.DEFAULT_SCOPE
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor or not sys.stdin.isatty():
        raise workflows.WorkflowError("no editor here: change it with `cauce workflow step set|add|rm`, or "
                                      f"`cauce workflow show {args.name} --json` and `cauce workflow save --replace`")
    draft = Path(defn["path"]) if where == defn["scope"] else None
    if draft is None:
        draft = workflows.save(defn, _cwd(args), where)
        print(f"copied the bundled template to the {where} scope: {draft}")
    before = draft.read_text(encoding="utf-8")
    subprocess.run([*shlex.split(editor), str(draft)], check=False)
    try:
        workflows.normalize(json.loads(draft.read_text(encoding="utf-8")), args.name)
    except (ValueError, workflows.WorkflowError) as exc:
        draft.write_text(before, encoding="utf-8")
        raise workflows.WorkflowError(f"not kept, the file is as it was: {exc}") from exc
    _say_saved(where, draft)
    return 0


def _run(args: argparse.Namespace) -> int:
    where = _cwd(args)
    inputs = dict(kv.split("=", 1) for kv in args.input or () if "=" in kv)
    bad = [kv for kv in args.input or () if "=" not in kv]
    if bad:
        raise workflows.WorkflowError(f"--input takes name=value, not {bad[0]!r}")
    if args.text is not None:
        defn = workflows.load(args.name, where, args.scope)
        if len(defn["inputs"]) != 1:
            raise workflows.WorkflowError(f"{args.name} takes {defn['inputs'] or 'no input'}: use --input name=value")
        inputs[defn["inputs"][0]] = sys.stdin.read() if args.text == "-" else args.text
    options = {k: v for k, v in {"budget_usd": args.budget, "allow_tools": args.allow,
                                 "allow_approval": args.allow_approval or None}.items() if v is not None}
    with contextlib.suppress(OSError, project.NotAProject):
        project.enroll(where)
    store = Store.open()
    try:
        run = workflows.start(store, args.name, where, inputs, scope=args.scope, options=options,
                              session_id=os.environ.get("CAUCE_SESSION_ID"))
        shown = workflows.status(store, run["id"])
    finally:
        store.close()
    if args.json:
        print(json.dumps(shown, ensure_ascii=False, default=str))
    else:
        print(f"started run #{run['id']} of {run['name']} ({run['scope']}): "
              f"{', '.join(shown['now']) or 'nothing'} queued; each next step starts when the one before passes.")
        print(f"where it is: `cauce workflow status {run['id']}`")
    return 0


def _status(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.run is not None:
            found = [workflows.status(store, args.run)]
        else:
            from cauce import repo

            keys = None if args.all else {repo.key(_cwd(args))}
            found = workflows.runs(store, repos=keys, limit=args.limit)
    finally:
        store.close()
    if args.json:
        print(json.dumps(found if args.run is None else found[0], ensure_ascii=False, default=str))
        return 0
    if not found:
        print("no workflow run here; `cauce workflow list` shows what can run")
    for n, run in enumerate(found):
        if n:
            print()
        _print_run(run)
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    top = sub.add_parser("workflow", help="saved chains of tasks that go on by themselves: make, change, run, watch")
    wsub = top.add_subparsers(dest="workflow_command")
    top.set_defaults(func=cmd_workflow, workflow_command="list", repo=None, json=False)

    def scoped(p: argparse.ArgumentParser, default: str | None = None) -> None:
        p.add_argument("--scope", choices=workflows.SCOPES if default is None else ("project", "user"), default=default,
                       help="project (.cauce/workflows), user (cauce home) or bundled; nearest first when left out"
                       if default is None else "where it is written (default: user)")
        p.add_argument("--repo", help="the project directory (default: here)")

    p = wsub.add_parser("list", help="every workflow, nearest scope first")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p = wsub.add_parser("show", help="one workflow's steps")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    scoped(p)
    p = wsub.add_parser("new", help="a new workflow: one step to start from, or a copy (--from)")
    p.add_argument("name")
    p.add_argument("--description")
    p.add_argument("--from", dest="copy_from", metavar="WORKFLOW", help="start from this one instead")
    scoped(p, workflows.DEFAULT_SCOPE)
    p = wsub.add_parser("copy", help="a workflow under a new name, to change without starting over")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--from-scope", choices=workflows.SCOPES)
    scoped(p, workflows.DEFAULT_SCOPE)
    p = wsub.add_parser("save", help="write a definition from a JSON file (- for stdin), checked first")
    p.add_argument("file")
    p.add_argument("--name", help="save it under this name")
    p.add_argument("--replace", action="store_true", help="overwrite one of the same name in that scope")
    scoped(p, workflows.DEFAULT_SCOPE)
    p = wsub.add_parser("edit", help="open it in $EDITOR; kept only if it is still valid")
    p.add_argument("name")
    scoped(p)
    p = wsub.add_parser("rm", help="remove a saved workflow")
    p.add_argument("name")
    p.add_argument("--scope", choices=("project", "user"), required=True)
    p.add_argument("--repo")
    p = wsub.add_parser("validate", help="check a saved workflow, or a file (--file)")
    p.add_argument("name", nargs="?")
    p.add_argument("--file")
    scoped(p)

    step = wsub.add_parser("step", help="add, change or remove one step")
    ssub = step.add_subparsers(dest="step_command", required=True)
    for verb, helptext in (("add", "a new step (after the last, or --after STEP)"),
                           ("set", "change a step's fields (--clear KEY removes one)"),
                           ("rm", "remove a step; what waited on it waits on what it waited on")):
        p = ssub.add_parser(verb, help=helptext)
        p.add_argument("name")
        p.add_argument("step_id", metavar="STEP")
        scoped(p)
        if verb == "rm":
            continue
        for flag, kind in STEP_FLAGS:
            p.add_argument(f"--{flag.replace('_', '-')}", dest=flag, type=kind,
                           help="the task text; {input} names are replaced at run time; - reads stdin"
                           if flag == "prompt" else None)
        p.add_argument("--needs", help="comma-separated step ids it waits on ('' for none)")
        p.add_argument("--memory", help="memory its brief carries, comma-separated: note:<id> (or <id>), "
                       "topic:<name>, problem:<id>")
        if verb == "add":
            p.add_argument("--after", metavar="STEP", help="insert it after this step")
        else:
            p.add_argument("--clear", action="append", choices=[*(f for f, _ in STEP_FLAGS if f != "prompt"), "memory"])

    p = wsub.add_parser("run", help="start a run here: its first steps are queued and the rest follow by themselves")
    p.add_argument("name")
    p.add_argument("text", nargs="?", help="the value of its one input (- reads stdin)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE")
    p.add_argument("--budget", type=float, help="dollars each step may spend (default: the step's own, else 5)")
    p.add_argument("--allow", action="append", metavar="RULE", help="a permission rule every step's workers get")
    p.add_argument("--allow-approval", action="store_true", help="steps may reach cells that need approval (Fable)")
    p.add_argument("--json", action="store_true")
    scoped(p)
    p = wsub.add_parser("status", help="where runs are: each step's state and task (this repository; --all)")
    p.add_argument("run", type=int, nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p = wsub.add_parser("cancel", help="stop a run: no further step starts")
    p.add_argument("run", type=int)
    p = wsub.add_parser("retry", help="queue a stopped step again; the run goes on from there")
    p.add_argument("run", type=int)
    p.add_argument("step")
    for parser in wsub.choices.values():
        parser.set_defaults(func=cmd_workflow)
