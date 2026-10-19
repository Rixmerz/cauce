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
import tempfile
from pathlib import Path
from typing import Any

from cauce import after, project, workflows
from cauce.store import Store

NOW_HELP = ("write the change at once even while runs use the workflow (they go on with the copy they started "
            "with); without it the change waits for them")
STEP_FLAGS = (("title", str), ("prompt", str), ("kind", str), ("role", str), ("verify", str), ("start", str),
              ("max_turns", int), ("ledger", str), ("fanout", str))


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
    if getattr(args, "surfaces", None) is not None:
        out["surfaces"] = [m.strip() for m in args.surfaces.split(",") if m.strip()] or None
    if getattr(args, "integrate", False):
        out["integrate"] = True
    return out


def _say_saved(where: str, path: Path | workflows.Saved) -> None:
    if isinstance(path, workflows.Saved) and path.held_by:
        runs = ", ".join(f"#{r}" for r in path.held_by)
        print(f"in use by run {runs}: the change is kept as a pending revision ({path.path}) and applies when they "
              "end — they go on with the copy they started with. `cauce workflow pending` lists it; --now applies "
              "it at once.")
        return
    print(f"saved in the {where} scope: {path.path if isinstance(path, workflows.Saved) else path}")


def _print_definition(defn: dict[str, Any]) -> None:
    print(f"{defn['name']} v{defn['version']} ({defn['scope']}: {defn['path']})")
    if defn.get("held_by"):
        print(f"  in use by run {', '.join(f'#{r}' for r in defn['held_by'])}: a change waits for them to end")
    if defn.get("pending"):
        print(f"  a pending change waits: {defn['pending']} (`cauce workflow pending {defn['name']} --apply|--drop`)")
    if defn.get("description"):
        print(f"  {defn['description']}")
    print(f"  inputs: {', '.join(defn['inputs']) or 'none'}")
    for step in defn["steps"]:
        after = f" after {', '.join(step['needs'])}" if step["needs"] else " (first)"
        extra = ", ".join(f"{k} {step[k]}" for k in ("kind", "start", "verify", "max_turns", "fanout")
                          if step.get(k) is not None) + (", integrate" if step.get("integrate") else "")
        print(f"\n  {step['id']} — {step['title']}{after}" + (f" [{extra}]" if extra else ""))
        for line in step["prompt"].splitlines():
            print(f"      {line}")
        if step.get("memory"):
            print(f"      + memory: {', '.join(step['memory'])}")


MARK = {"done": "✓", "running": "▶", "queued": "…", "pending": "·"}


def _files(found: list[str], total: int | None = None) -> str:
    """The count is the task's final ending's (`total`); `found` may list only the head of them."""
    total = total or len(found)
    if not total:
        return ""
    shown = ", ".join(found[:3]) + (f" +{total - 3}" if total > 3 else "")
    return f"{total} file(s): {shown}"


def _step_line(step: dict[str, Any]) -> str:
    """One step's progress: its state, task and attempt, what it is doing, its commit, its files."""
    parts = [step["state"] + (f" #{step['task']}" if step.get("task") else "")]
    if step.get("attempt"):
        parts.append(f"attempt {step['attempt']}" + (f" at {step['cell']}" if step.get("cell") else ""))
    elif step.get("cell"):
        parts.append(f"at {step['cell']}")
    if step.get("tries", 1) > 1:
        parts.append(f"try {step['tries']}")
    if step.get("phase"):
        parts.append(step["phase"])
    if step.get("waits"):
        parts.append(step["waits"])
    if step.get("commit"):
        parts.append(step["commit"][:72])
    if step.get("files"):
        parts.append(_files(step["files"], step.get("files_n")))
    if step.get("behind"):
        parts.append(step["behind"])
    return " · ".join(parts)


def _points(run: dict[str, Any]) -> str:
    return f" · {run['points']} points" if run.get("points") else ""


def _run_line(run: dict[str, Any]) -> str:
    """One run on one line: the step it is at, its attempt, its last commit, its files."""
    steps = run["steps"]
    at = next((s for s in steps if s["id"] in run["waiting"]), None) or next(
        (s for s in steps if s["state"] in ("running", "queued")), None) or next(
        (s for s in steps if s["state"] not in ("done", "pending")), None) or next(
        (s for s in reversed(steps) if s["state"] == "done"), steps[-1] if steps else None)
    head = f"#{run['id']} {run['name']} — {run['state']} · ${run['cost_usd']:.2f}" + _points(run)
    if at is None:
        return head
    n = steps.index(at) + 1
    last = next((s.get("commit") for s in reversed(steps) if s.get("commit")), None)
    rest = [f"step {at['id']} ({n}/{len(steps)}): {_step_line(at)}"]
    if last and last != at.get("commit"):
        rest.append(f"last commit {last[:60]}")
    return head + " · " + " · ".join(rest)


def _print_run(run: dict[str, Any], *, full: bool = False) -> None:
    print(f"run #{run['id']} {run['name']} — {run['state']} · ${run['cost_usd']:.2f}{_points(run)} · {run['cwd']}")
    for name, value in run["inputs"].items():
        # An input is often a whole brief: one line of it, unless asked for in full.
        print(f"  {name}: {value if full else _clip(value)}")
    for step in run["steps"]:
        print(f"  {MARK.get(step['state'], '!')} {step['id']:<14} {_step_line(step)}")
        for part in step.get("parts") or ():
            print(f"    {MARK.get(part['state'], '!')} {part['id']:<12} {_step_line(part)}")
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
                if not w.get("error"):
                    # What a run asks for, so the first try is the one that starts it.
                    needs = [f"inputs: {', '.join(w['inputs'])}" if w["inputs"] else "no input"]
                    if w.get("memory"):
                        needs.append(f"memory: {', '.join(w['memory'])}")
                    print(f"{'':<27} {' · '.join(needs)}")
            return 0
        if what == "show":
            defn = workflows.load(args.name, _cwd(args), args.scope)
            if defn["scope"] != "bundled":
                waiting = workflows.pending_path(Path(defn["path"]))
                defn.update(held_by=workflows.in_use(defn["path"]),
                            pending=str(waiting) if waiting.is_file() else None)
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
            _say_saved(args.scope, workflows.save(defn, _cwd(args), args.scope, replace=args.replace, now=args.now))
            return 0
        if what == "edit":
            return _edit_in_editor(args)
        if what == "rm":
            print(f"removed {workflows.remove(args.name, _cwd(args), args.scope, force=args.force)}")
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
        if what == "pending":
            if args.name and args.apply:
                print(f"applied: {workflows.apply_pending(args.name, _cwd(args), args.scope, now=args.now)}")
                return 0
            if args.name and args.drop:
                print(f"dropped the pending change: {workflows.drop_pending(args.name, _cwd(args), args.scope)}")
                return 0
            found = [w for w in workflows.pending(_cwd(args)) if not args.name or w["name"] == args.name]
            if args.json:
                print(json.dumps(found, ensure_ascii=False))
                return 0
            for w in found:
                held = (f"waits for run {', '.join(f'#{r}' for r in w['held_by'])}" if w["held_by"]
                        else "nothing uses it now: --apply")
                print(f"{w['name']:<18} {w['scope']:<8} {held}")
            if not found:
                print("no change waits for a run to end")
            return 0
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
                if task.get("parts_retried"):
                    print(f"run #{args.run}: step {args.step}: " + ", ".join(
                        f"{part} queued again as #{tid}" for part, tid in task["parts_retried"]))
                else:
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
    saved = workflows.edit(args.name, _cwd(args), change, args.scope, now=args.now)
    _say_saved(saved.scope, saved)
    return 0


def _edit_in_editor(args: argparse.Namespace) -> int:
    """The definition in $EDITOR; checked when the editor closes, and kept only if it holds."""
    defn = workflows.load(args.name, _cwd(args), args.scope)
    where = defn["scope"] if defn["scope"] != "bundled" else workflows.DEFAULT_SCOPE
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor or not sys.stdin.isatty():
        raise workflows.WorkflowError("no editor here: change it with `cauce workflow step set|add|rm`, or "
                                      f"`cauce workflow show {args.name} --json` and `cauce workflow save --replace`")
    # The person edits a copy; what they keep is saved like any change, so a
    # workflow in use gets a pending revision instead of changing under its runs.
    waiting = workflows.pending_path(Path(defn["path"]))
    source = waiting if where == defn["scope"] and waiting.is_file() else Path(defn["path"])
    draft = Path(tempfile.mkdtemp(prefix="cauce-workflow-")) / f"{args.name}.json"
    draft.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run([*shlex.split(editor), str(draft)], check=False)
    try:
        changed = workflows.normalize(json.loads(draft.read_text(encoding="utf-8")), args.name)
    except (ValueError, workflows.WorkflowError) as exc:
        raise workflows.WorkflowError(f"not kept, nothing changed: {exc} (your edit is in {draft})") from exc
    _say_saved(where, workflows.save(changed, _cwd(args), where, replace=True, now=args.now))
    return 0


def _inputs(args: argparse.Namespace, where: Path) -> dict[str, str]:
    """The run's inputs: `--input name=value`, and the text given without a name
    for the one input left (`-` reads stdin)."""
    inputs = dict(kv.split("=", 1) for kv in args.input or () if "=" in kv)
    bad = [kv for kv in args.input or () if "=" not in kv]
    if bad:
        raise workflows.WorkflowError(f"--input takes name=value, not {bad[0]!r}")
    if args.text is not None:
        defn = workflows.load(args.name, where, args.scope)
        left = [i for i in defn["inputs"] if i not in inputs]
        if len(left) != 1:
            takes = f"takes {', '.join(defn['inputs'])}" if defn["inputs"] else "takes no input"
            raise workflows.WorkflowError(f"{args.name} {takes}: name each one, `{workflows.example(defn)}`")
        inputs[left[0]] = sys.stdin.read() if args.text == "-" else args.text
    return inputs


def _clip(text: str, chars: int = 100) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= chars else one[:chars - 1] + "…"


def _run(args: argparse.Namespace) -> int:
    where = _cwd(args)
    inputs = _inputs(args, where)
    options = {k: v for k, v in {"allow_tools": args.allow, "allow_approval": args.allow_approval or None,
                                 "verify": args.verify,
                                 # A directory beside the work every step's workers may read, kept on the run.
                                 "add_dirs": [str(Path(d).expanduser().resolve()) for d in args.add_dir]
                                 if getattr(args, "add_dir", None) else None}.items() if v is not None}
    store = Store.open()
    try:
        if args.dry_run:
            if args.after is not None:
                after.target(store, run_id=args.after)  # the run it would wait for is checked too
            ready = workflows.prepare(store, args.name, where, inputs, scope=args.scope)
            if args.json:
                print(json.dumps({"name": ready["defn"]["name"], "inputs": ready["given"], "gaps": ready["gaps"],
                                  "steps": [{"id": s["id"], "kind": s.get("kind"), "needs": s["needs"],
                                             "verify": workflows.step_check(s, options)}
                                            for s in ready["defn"]["steps"]]}, ensure_ascii=False))
                return 0
            print(f"{ready['defn']['name']} would run here ({where}); nothing is queued:")
            for name, value in ready["given"].items():
                print(f"  {name}: {_clip(value)}")
            for step in ready["defn"]["steps"]:
                needs = f" after {', '.join(step['needs'])}" if step["needs"] else ""
                check = workflows.step_check(step, options)
                later = not check and not step.get("kind") and options.get("verify") and not step.get("verify")
                print(f"  · {step['id']} ({step.get('kind') or 'classified when it runs'}){needs}"
                      + (f", checked by `{check}`" if check else "")
                      + (f", checked by `{options['verify']}` if it writes" if later else ""))
            for gap in ready["gaps"]:
                print(f"  note: {gap}")
            return 0
        with contextlib.suppress(OSError, project.NotAProject):
            project.enroll(where)
        run = workflows.start(store, args.name, where, inputs, scope=args.scope, options=options,
                              session_id=os.environ.get("CAUCE_SESSION_ID"), after_run=args.after)
        shown = workflows.status(store, run["id"])
        unseen = None
        try:
            warned = _first_warnings(store, run["queued"])
        except Exception as exc:  # the run is queued: a plan that cannot be foreseen never undoes its start
            warned, unseen = [], f"{type(exc).__name__}: {exc}"
    finally:
        store.close()
    if args.json:
        print(json.dumps({**shown, "gaps": run["gaps"], "warnings": warned,
                          **({"warnings_error": unseen} if unseen else {})}, ensure_ascii=False, default=str))
    else:
        print(f"started run #{run['id']} of {run['name']} ({run['scope']}): "
              f"{', '.join(shown['now']) or 'nothing'} queued; each next step starts when the one before passes.")
        if shown.get("waits"):
            print(f"it {shown['waits']}")
        for gap in run["gaps"]:
            print(f"note: {gap}")
        if unseen:
            print(f"note: the warnings of the first steps could not be foreseen ({unseen})")
        for warning in warned:
            print(f"warning: step {warning['step']} (#{warning['task']}): {warning['warning']}")
        print(f"where it is: `cauce workflow status {run['id']}`")
    return 0


def _first_warnings(store: Store, queued: list[int]) -> list[dict[str, Any]]:
    """What the plans of the steps a run starts with will warn of, said now: the
    run's caller is gone by the time a step starts and its `planned` event holds them."""
    from cauce import flow, orchestrate

    out = []
    for task_id in queued:
        task = store.get_task(task_id)
        options = flow.queued_options(task, orchestrate.Options())
        out += [{"step": task["workflow_step"], "task": task_id, "warning": warning}
                for warning in orchestrate.foresee(task["body"], Path(task["cwd"]), store, options)]
    return out


def _status(args: argparse.Namespace) -> int:
    store = Store.open()
    try:
        if args.run is not None:
            found = [workflows.status(store, args.run, detail=True)]
        else:
            from cauce import repo

            keys = None if args.all else {repo.key(_cwd(args))}
            found = [workflows.status(store, r["id"], detail=True)
                     for r in store.list_runs(repos=keys, limit=args.limit)]
    finally:
        store.close()
    if args.json:
        print(json.dumps(found if args.run is None else found[0], ensure_ascii=False, default=str))
        return 0
    if not found:
        print("no workflow run here; `cauce workflow list` shows what can run")
        return 0
    if args.run is None and not args.full:
        for run in found:
            print(_run_line(run))
        print("one run in full: `cauce workflow status <run>`")
        return 0
    for n, run in enumerate(found):
        if n:
            print()
        _print_run(run, full=args.full)
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
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    scoped(p, workflows.DEFAULT_SCOPE)
    p = wsub.add_parser("edit", help="open it in $EDITOR; kept only if it is still valid")
    p.add_argument("name")
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    scoped(p)
    p = wsub.add_parser("rm", help="remove a saved workflow")
    p.add_argument("name")
    p.add_argument("--scope", choices=("project", "user"), required=True)
    p.add_argument("--repo")
    p.add_argument("--force", action="store_true", help="remove it although runs use it (they keep their copy)")
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
        p.add_argument("--now", action="store_true", help=NOW_HELP)
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
        p.add_argument("--surfaces", help="comma-separated paths or globs the step may change")
        p.add_argument("--integrate", action="store_true",
                       help="join the branches the parts of a fanout step before it kept, in the plan's order")
        if verb == "add":
            p.add_argument("--after", metavar="STEP", help="insert it after this step")
        else:
            p.add_argument("--clear", action="append",
                           choices=[*(f for f, _ in STEP_FLAGS if f != "prompt"), "memory", "surfaces",
                                    "integrate"])

    p = wsub.add_parser("pending", help="changes waiting for the runs that use their workflow to end")
    p.add_argument("name", nargs="?")
    p.add_argument("--apply", action="store_true", help="apply it now that nothing uses it (with --now, anyway)")
    p.add_argument("--drop", action="store_true", help="throw the waiting change away")
    p.add_argument("--now", action="store_true", help=NOW_HELP)
    p.add_argument("--json", action="store_true")
    scoped(p)
    p = wsub.add_parser("run", help="start a run here: its first steps are queued and the rest follow by themselves",
                        description="`cauce workflow list` shows what each workflow takes; --dry-run checks it all "
                                    "first and queues nothing.")
    p.add_argument("name")
    p.add_argument("text", nargs="?", help="the value of the one input not given with --input (- reads stdin)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", help="one input by name (repeatable)")
    p.add_argument("--verify", metavar="CMD", help="the check for every step that writes and names none of its own")
    p.add_argument("--allow", action="append", metavar="RULE", help="a permission rule every step's workers get")
    p.add_argument("--allow-approval", action="store_true", help="steps may reach cells that need approval (Fable)")
    p.add_argument("--add-dir", action="append", default=[], metavar="DIR",
                   help="a directory beside the work every step's workers may use, such as a sibling repository "
                        "(repeatable)")
    p.add_argument("--dry-run", action="store_true", help="check the inputs and the memory, show the steps; queue "
                                                          "nothing")
    p.add_argument("--after", type=int, metavar="RUN",
                   help="queue its first steps held until workflow run RUN passes, then start them on the commit its "
                        "last step kept (on the base when that is merged); nothing is merged or rebased")
    p.add_argument("--json", action="store_true")
    scoped(p)
    p = wsub.add_parser("status", help="where runs are: a line per run (this project; --all), or one run step "
                                       "by step")
    p.add_argument("run", type=int, nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--full", action="store_true", help="every run step by step, its inputs whole")
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
