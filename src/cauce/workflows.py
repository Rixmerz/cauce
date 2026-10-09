"""Workflows: saved chains of cauce tasks that advance on their own.

A workflow is a definition — named steps, each a task text with what it needs
before it — kept as JSON in one of three places, the nearest winning:

- `project`: `<checkout>/.cauce/workflows/<name>.json`, beside the project's code
- `user`: `<cauce home>/workflows/<name>.json`, the person's own
- `bundled`: the templates that ship with cauce

A run of one is not driven by a session. Its first steps are queued as
ordinary tasks; when a step passes, the engine itself (`advance`, called at the
end of every run) queues the steps that were waiting on it, each starting its
worktree from the commit the step before it kept, and starts the repository's
dispatcher. Every step keeps cauce's own guarantees: its cell, its check, its
escalation, its stops. A step that stops for a person stops the run there until
it is resumed, retried or the run is cancelled; resuming it to a pass goes on.

The session that started a run hears of it once at its end, or when a step
waits on the person: the endings of steps the engine went past are marked
reported. A watcher reads where a run is with `status`, and changes nothing.

A definition grants nothing: no permission rule, no approval, no capability.
Those stay a person's flags on `cauce workflow run`, so a workflow committed
to a repository runs with exactly what the person running it gives.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cauce import dispatch, isolate, project, repo
from cauce.matrix import KINDS, Cell
from cauce.store import Store, home

SCOPES = ("project", "user", "bundled")
#: Where a new or copied workflow goes when the caller does not say.
DEFAULT_SCOPE = "user"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
INPUT = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
TOP_KEYS = frozenset({"name", "description", "inputs", "steps"})
#: What a step may say. Nothing here widens what a worker may do: rules,
#: approvals and capabilities are the person's flags on `run`, never a file's.
STEP_KEYS = frozenset({"id", "title", "prompt", "kind", "needs", "verify", "start", "budget_usd", "max_turns"})
PROMPT_CHARS = 8000
RESULT_CHARS = 1500

#: Step states as `status` shows them.
NEEDS_YOU = frozenset({"failed", "blocked", "replan", "needs_approval", "interrupted"})
STOPPED = frozenset({"cancelled", "dismissed"})


class WorkflowError(ValueError):
    """A definition, a name or a run that cannot be used as asked; the message says why."""


# --- where definitions live --------------------------------------------------------


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "workflow_templates"


def dirs(cwd: str | Path | None) -> dict[str, Path | None]:
    """Each scope's folder. A directory outside an enrolled project has no project scope."""
    folder = project.find(cwd) if cwd else None
    return {"project": folder / "workflows" if folder else None, "user": home() / "workflows",
            "bundled": bundled_dir()}


def listing(cwd: str | Path | None) -> list[dict[str, Any]]:
    """Every workflow in every scope, nearest first; one a nearer scope hides is marked `shadowed`."""
    out, seen = [], set()
    for scope in SCOPES:
        folder = dirs(cwd)[scope]
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            name = path.stem
            if not NAME.match(name):
                continue
            entry: dict[str, Any] = {"name": name, "scope": scope, "path": str(path), "shadowed": name in seen}
            try:
                defn = normalize(_read(path), name)
                entry.update(description=defn.get("description", ""), steps=[s["id"] for s in defn["steps"]],
                             inputs=defn.get("inputs", []))
            except WorkflowError as exc:
                entry.update(error=str(exc))
            seen.add(name)
            out.append(entry)
    return out


def locate(name: str, cwd: str | Path | None, scope: str | None = None) -> tuple[str, Path]:
    _check_name(name)
    for where in [scope] if scope else SCOPES:
        if where not in SCOPES:
            raise WorkflowError(f"no scope {where!r}: one of {', '.join(SCOPES)}")
        folder = dirs(cwd)[where]
        if folder is not None and (folder / f"{name}.json").is_file():
            return where, folder / f"{name}.json"
    raise WorkflowError(f"no workflow {name!r}" + (f" in the {scope} scope" if scope else "")
                        + "; `cauce workflow list` shows them")


def load(name: str, cwd: str | Path | None, scope: str | None = None) -> dict[str, Any]:
    """A saved definition, checked, with where it came from (`scope`, `path`)."""
    where, path = locate(name, cwd, scope)
    return {**normalize(_read(path), name), "scope": where, "path": str(path)}


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path} holds no workflow object")
    return data


def _check_name(name: str) -> None:
    if not NAME.match(name or ""):
        raise WorkflowError(f"{name!r} is no workflow name: lowercase letters, digits, '.', '_' and '-'")


# --- the definition ------------------------------------------------------------------


def skeleton(name: str, description: str = "") -> dict[str, Any]:
    """A new workflow to fill: one step, which `step add` and `step set` change."""
    _check_name(name)
    return {"name": name, "description": description, "inputs": ["goal"],
            "steps": [{"id": "do", "prompt": "{goal}"}]}


def normalize(defn: Mapping[str, Any], name: str | None = None) -> dict[str, Any]:
    """The definition checked and completed: every step has `needs` (the step
    before it, when it does not say) and a title. Raises with every problem found."""
    errors: list[str] = []
    data = {k: copy.deepcopy(v) for k, v in defn.items() if k not in ("scope", "path")}
    unknown = set(data) - TOP_KEYS
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}: a workflow has {sorted(TOP_KEYS)}")
    data["name"] = name or data.get("name") or ""
    if not NAME.match(str(data["name"])):
        errors.append(f"{data['name']!r} is no workflow name")
    if not isinstance(data.get("description", ""), str):
        errors.append("description is text")
    inputs = data.get("inputs", [])
    if not isinstance(inputs, list) or not all(isinstance(i, str) and INPUT.fullmatch("{" + i + "}") for i in inputs):
        errors.append("inputs is a list of names (lowercase, digits, '_')")
        inputs = []
    data["inputs"] = list(dict.fromkeys(inputs))
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps is a list of at least one step")
        steps = []
    ids: list[str] = []
    for n, raw in enumerate(steps):
        where = f"step {n + 1}"
        if not isinstance(raw, dict):
            errors.append(f"{where} is not an object")
            continue
        step = dict(raw)
        steps[n] = step
        extra = set(step) - STEP_KEYS
        if extra:
            errors.append(f"{where} has unknown keys {sorted(extra)}: a step has {sorted(STEP_KEYS)}"
                          + (" (rules and approvals are flags of `cauce workflow run`, never of a file)"
                             if extra & {"allow", "allow_tools", "allow_approval", "capabilities"} else ""))
        sid = step.get("id")
        if not isinstance(sid, str) or not NAME.match(sid):
            errors.append(f"{where} needs an id (lowercase letters, digits, '.', '_', '-')")
            sid = f"#{n + 1}"
        elif sid in ids:
            errors.append(f"step id {sid!r} is used twice")
        where = f"step {sid!r}"
        prompt = step.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{where} needs a prompt: the task its worker is given")
        elif len(prompt) > PROMPT_CHARS:
            errors.append(f"{where}'s prompt is over {PROMPT_CHARS} characters")
        else:
            for used in INPUT.findall(prompt):
                if used not in data["inputs"]:
                    errors.append(f"{where} uses {{{used}}}, which is not among the inputs {data['inputs']}")
        if step.get("kind") is not None and step["kind"] not in KINDS:
            errors.append(f"{where}: no kind {step['kind']!r}; one of {', '.join(KINDS)}")
        if step.get("start") is not None:
            try:
                Cell.parse(str(step["start"]))
            except ValueError as exc:
                errors.append(f"{where}: start {step['start']!r} is no cell ({exc})")
        if step.get("verify") is not None and not isinstance(step["verify"], str):
            errors.append(f"{where}: verify is a command, as text")
        for key, kind in (("budget_usd", (int, float)), ("max_turns", int)):
            if step.get(key) is not None and (not isinstance(step[key], kind) or isinstance(step[key], bool)
                                              or step[key] <= 0):
                errors.append(f"{where}: {key} is a positive number")
        if "needs" not in step:
            step["needs"] = ids[-1:]
        if not isinstance(step["needs"], list) or not all(isinstance(x, str) for x in step["needs"]):
            errors.append(f"{where}: needs is a list of step ids")
            step["needs"] = []
        for dep in step["needs"]:
            if dep not in ids:
                errors.append(f"{where} needs {dep!r}, which is not a step before it")
        step["title"] = str(step.get("title") or sid)
        ids.append(sid)
    if errors:
        raise WorkflowError("; ".join(errors))
    data["steps"] = steps
    return data


def save(defn: Mapping[str, Any], cwd: str | Path | None, scope: str = DEFAULT_SCOPE, *,
         replace: bool = False) -> Path:
    """Write a definition, checked first. A bundled template is never written: copy it."""
    if scope not in ("project", "user"):
        raise WorkflowError(f"a workflow is saved in the project or the user scope, not {scope!r}")
    checked = normalize(defn)
    folder = dirs(cwd)[scope]
    if folder is None:
        raise WorkflowError("this directory is in no enrolled project (a .cauce/ folder): save it in the user scope, "
                            "or `cauce init` the project first")
    path = folder / f"{checked['name']}.json"
    if path.exists() and not replace:
        raise WorkflowError(f"{checked['name']!r} exists in the {scope} scope; edit it, or remove it first")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_stored(checked), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _stored(defn: Mapping[str, Any]) -> dict[str, Any]:
    """As written to a file: a title equal to the id and the implicit `needs` left out."""
    steps, before = [], []
    for step in defn["steps"]:
        out = {k: v for k, v in step.items() if v is not None}
        if out.get("title") == out["id"]:
            out.pop("title")
        if out.get("needs") == before[-1:]:
            out.pop("needs")
        steps.append(out)
        before.append(step["id"])
    return {"name": defn["name"], "description": defn.get("description", ""), "inputs": defn.get("inputs", []),
            "steps": steps}


def copy_to(source: str, target: str, cwd: str | Path | None, *, scope: str = DEFAULT_SCOPE,
            from_scope: str | None = None) -> Path:
    """A saved workflow under a new name, to edit without starting from nothing."""
    defn = load(source, cwd, from_scope)
    _check_name(target)
    return save({**defn, "name": target}, cwd, scope)


def remove(name: str, cwd: str | Path | None, scope: str) -> Path:
    if scope == "bundled":
        raise WorkflowError("a bundled template is not removed; a workflow of the same name in the user or project "
                            "scope hides it")
    _, path = locate(name, cwd, scope)
    path.unlink()
    return path


def edit(name: str, cwd: str | Path | None, change: Callable[[dict[str, Any]], None],
         scope: str | None = None) -> tuple[str, Path]:
    """Load, change, check, write back where it was. A bundled template is copied
    to the user scope first: the change is the person's, the template stays."""
    defn = load(name, cwd, scope)
    where = defn["scope"] if defn["scope"] != "bundled" else DEFAULT_SCOPE
    change(defn)
    return where, save(defn, cwd, where, replace=True)


def add_step(defn: dict[str, Any], step: dict[str, Any], *, after: str | None = None) -> None:
    ids = [s["id"] for s in defn["steps"]]
    if step.get("id") in ids:
        raise WorkflowError(f"step {step['id']!r} exists; `step set` changes it")
    at = len(ids) if after is None else _index(defn, after) + 1
    if "needs" not in step and at < len(ids):
        # Inserted in a chain: it needs what was before it, and what came after it now needs it.
        step["needs"] = [ids[at - 1]] if at else []
        follower = defn["steps"][at]
        if follower.get("needs") == step["needs"]:
            follower["needs"] = [step["id"]]
    defn["steps"].insert(at, step)


def set_step(defn: dict[str, Any], step_id: str, changes: Mapping[str, Any]) -> None:
    step = defn["steps"][_index(defn, step_id)]
    for key, value in changes.items():
        if value is None:
            step.pop(key, None)
        else:
            step[key] = value


def remove_step(defn: dict[str, Any], step_id: str) -> None:
    at = _index(defn, step_id)
    gone = defn["steps"].pop(at)
    for step in defn["steps"]:
        if step_id in step.get("needs", []):
            # What waited on it waits on what it waited on.
            step["needs"] = list(dict.fromkeys(n for dep in step["needs"]
                                               for n in (gone.get("needs", []) if dep == step_id else [dep])))


def _index(defn: Mapping[str, Any], step_id: str) -> int:
    for n, step in enumerate(defn["steps"]):
        if step.get("id") == step_id:
            return n
    raise WorkflowError(f"no step {step_id!r}; the steps are {[s.get('id') for s in defn['steps']]}")


# --- runs ----------------------------------------------------------------------------

Kick = Callable[[str, str | None], object]


def _kick(cwd: str, key: str | None) -> bool:
    """Start the repository's dispatcher unless one runs. A workflow run is the
    person asking for its work, so this does not wait for the `autowork` setting."""
    root = home()
    return dispatch.held(root, key or "*") or dispatch.start(Path(cwd), root, key or "*")


def start(store: Store, name: str, cwd: str | Path, inputs: Mapping[str, str] | None = None, *,
          scope: str | None = None, session_id: str | None = None, options: Mapping[str, Any] | None = None,
          kick: Kick | None = None) -> dict[str, Any]:
    """A run of a saved workflow in a directory: its first steps queued, the dispatcher started.
    `options` are the person's (`budget_usd` per step, `allow_tools`, `allow_approval`)."""
    defn = load(name, cwd, scope)
    where = Path(cwd).resolve()
    given = {k: str(v) for k, v in (inputs or {}).items()}
    missing = [i for i in defn["inputs"] if not given.get(i, "").strip()]
    if missing:
        raise WorkflowError(f"{name} needs {', '.join(missing)}: pass --input {missing[0]}=...")
    extra = sorted(set(given) - set(defn["inputs"]))
    if extra:
        raise WorkflowError(f"{name} takes no input {', '.join(extra)}; it takes {defn['inputs'] or 'none'}")
    key = repo.key(where)
    if repo.toplevel(where) is not None:
        # Where the run started: every step's brief can say what the run changed since.
        with contextlib.suppress(isolate.IsolationError):
            options = {**dict(options or {}), "origin": isolate.head(where, "HEAD")}
    run = store.create_run(name=defn["name"], scope=defn["scope"], source=defn["path"],
                           definition=json.dumps(_stored(defn), ensure_ascii=False),
                           inputs=json.dumps(given, ensure_ascii=False), options=json.dumps(dict(options or {})),
                           repo=key, cwd=str(where), session_id=session_id)
    queued = _queue_ready(store, run)
    (kick or _kick)(str(where), key)
    return {**run, "queued": queued}


def _definition(run: Mapping[str, Any]) -> dict[str, Any]:
    return normalize(json.loads(run["definition"]))


def _latest(store: Store, run_id: int) -> dict[str, dict]:
    """Each step's newest task: a retried step's last try is the one that counts."""
    out: dict[str, dict] = {}
    for task in store.run_tasks(run_id):
        out[task["workflow_step"]] = task
    return out


def _queue_ready(store: Store, run: Mapping[str, Any]) -> list[int]:
    """Queue every step whose needs all passed and that has no task yet."""
    if run["status"] != "active":
        return []
    defn, latest = _definition(run), _latest(store, run["id"])
    inputs, options = json.loads(run["inputs"]), json.loads(run["options"])
    queued = []
    for step in defn["steps"]:
        if step["id"] in latest or any((latest.get(d) or {}).get("status") != "done" for d in step["needs"]):
            continue
        queued.append(_queue_step(store, run, defn, step, inputs, options, latest)["id"])
    return queued


def _queue_step(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step: Mapping[str, Any],
                inputs: Mapping[str, str], options: Mapping[str, Any], latest: Mapping[str, dict]) -> dict:
    body = INPUT.sub(lambda m: inputs.get(m.group(1), m.group(0)), step["prompt"]).strip()
    earlier = _before(defn, step["id"])
    notes = []
    for sid in earlier:
        task = latest.get(sid)
        if task and task.get("result"):
            result = task["result"] if len(task["result"]) <= RESULT_CHARS else task["result"][:RESULT_CHARS] + "…"
            notes.append(f"- {sid} (task #{task['id']}): {result}")
    if notes:
        body += (f"\n\nThis is step {step['id']!r} of the workflow {defn['name']!r}. What the steps before it "
                 "found and did (their changes are already in this checkout):\n" + "\n".join(notes))
    base = _base(store, run, defn, step["id"], latest)
    if base and options.get("origin"):
        body += ("\n\nEverything this workflow changed so far is in this checkout: "
                 f"`git diff {options['origin'][:12]}` shows it.")
    opts = {k: v for k, v in {
        "kind": step.get("kind"), "verify": step.get("verify"), "start": step.get("start"),
        "budget_usd": step.get("budget_usd") or options.get("budget_usd"), "max_turns": step.get("max_turns"),
        "allow_tools": options.get("allow_tools") or None, "allow_approval": options.get("allow_approval") or None,
        "base": base}.items() if v is not None}
    return store.create_task(body, status="queued", source="queue", author="workflow", repo=run["repo"],
                             cwd=run["cwd"], session_id=run["session_id"], options=json.dumps(opts),
                             title=f"[{defn['name']}#{run['id']} {step['id']}] {step['title']}"[:200],
                             workflow_run=run["id"], workflow_step=step["id"])


def _before(defn: Mapping[str, Any], step_id: str) -> list[str]:
    """Every step a step waits on, directly or not, in definition order."""
    needs = {s["id"]: s["needs"] for s in defn["steps"]}
    seen, todo = set(), list(needs[step_id])
    while todo:
        sid = todo.pop()
        if sid not in seen:
            seen.add(sid)
            todo.extend(needs[sid])
    return [s["id"] for s in defn["steps"] if s["id"] in seen]


def _base(store: Store, run: Mapping[str, Any], defn: Mapping[str, Any], step_id: str,
          latest: Mapping[str, dict]) -> str | None:
    """The commit a step starts from: the tip the nearest step before it kept,
    read when it passed. A reading step keeps no branch, so the search goes on
    past it. None: the checkout's HEAD."""
    for sid in reversed(_before(defn, step_id)):
        task = latest.get(sid)
        finished = store.last_event(task["id"], "finished") if task else None
        branch = (finished or {}).get("data", {}).get("branch")
        if branch and run["cwd"] and Path(run["cwd"]).is_dir():
            try:
                return isolate.head(Path(run["cwd"]), branch)
            except isolate.IsolationError:
                continue
    return None


def advance(store: Store, task: Mapping[str, Any] | None, env: Mapping[str, str] | None = None,
            kick: Kick | None = None) -> list[int]:
    """After a step's run ended: when it passed, queue what waited on it and
    start the dispatcher. The session that started the run is told at its end,
    or when a step waits on the person, never of each step the engine went past."""
    if not task or not task.get("workflow_run"):
        return []
    run = store.get_run(int(task["workflow_run"]))
    if run is None or run["status"] != "active" or task["status"] != "done":
        return []
    # Two steps a third waits on may pass at the same moment: one of them queues it.
    with store.immediate():
        queued = _queue_ready(store, run)
        if queued:
            store.add_event(task["id"], "reported", via="workflow")
            store.add_event(task["id"], "workflow_advanced", run=run["id"], queued=queued)
    if queued:
        (kick or _kick)(run["cwd"], run["repo"])
    store.update_run(run["id"])
    return queued


def status(store: Store, run_id: int) -> dict[str, Any]:
    """Where a run is: each step's state and task, the steps running now, and the run's own state."""
    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    defn, latest = _definition(run), _latest(store, run_id)
    steps = []
    for step in defn["steps"]:
        task = latest.get(step["id"])
        state = "pending" if task is None else task["status"]
        entry: dict[str, Any] = {"id": step["id"], "title": step["title"], "needs": step["needs"], "state": state}
        if task:
            finished = store.last_event(task["id"], "finished")
            entry.update(task=task["id"], cell=task.get("final_cell") or task.get("current_cell"),
                         cost_usd=round(task.get("cost_usd") or 0, 2), result=task.get("result"),
                         branch=(finished or {}).get("data", {}).get("branch") if state == "done" else None,
                         tries=sum(1 for t in store.run_tasks(run_id) if t["workflow_step"] == step["id"]))
        steps.append(entry)
    states = {s["state"] for s in steps}
    if run["status"] == "cancelled":
        state = "cancelled"
    elif states == {"done"}:
        state = "done"
    elif states & NEEDS_YOU or (states & STOPPED and not states & {"queued", "running"}):
        state = "waits on you"
    elif states & {"queued", "running"}:
        state = "running"
    else:
        state = "stalled"
    branch = next((s["branch"] for s in reversed(steps) if s.get("branch")), None)
    return {"id": run["id"], "name": run["name"], "scope": run["scope"], "repo": run["repo"], "cwd": run["cwd"],
            "session_id": run["session_id"], "inputs": json.loads(run["inputs"]), "state": state,
            "now": [s["id"] for s in steps if s["state"] in ("queued", "running")],
            "waiting": [s["id"] for s in steps if s["state"] in NEEDS_YOU | STOPPED],
            "branch": branch, "cost_usd": round(sum(s.get("cost_usd") or 0 for s in steps), 2),
            "created_at": run["created_at"], "updated_at": run["updated_at"], "reason": run["reason"],
            "steps": steps}


def runs(store: Store, *, repos: set[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return [status(store, r["id"]) for r in store.list_runs(repos=repos, limit=limit)]


def cancel(store: Store, run_id: int, *, via: str = "cli") -> list[int]:
    """Stop a run: no step starts after this. Its queued steps are cancelled; a
    running one is asked to stop, as `cauce cancel` asks."""
    from cauce import stops

    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    store.update_run(run_id, status="cancelled", reason=f"cancelled {via}")
    stopped = []
    for task in _latest(store, run_id).values():
        if task["status"] == "queued":
            stops.record(store, task["id"], stops.Stop("cancelled", "its workflow run was cancelled", by="you",
                                                       extra={"via": via}))
            stopped.append(task["id"])
        elif task["status"] == "running":
            store.request_cancel(task["id"], via=via)
            if task.get("pid"):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(int(task["pid"]), signal.SIGTERM)
            stopped.append(task["id"])
    return stopped


def retry(store: Store, run_id: int, step_id: str, kick: Kick | None = None) -> dict:
    """Queue a step again as a new task, after its last try stopped: the run goes on from there."""
    run = store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"no workflow run #{run_id}")
    if run["status"] != "active":
        raise WorkflowError(f"run #{run_id} is {run['status']}")
    defn, latest = _definition(run), _latest(store, run_id)
    step = defn["steps"][_index(defn, step_id)]
    last = latest.get(step_id)
    if last is not None and last["status"] in ("queued", "running", "done"):
        raise WorkflowError(f"step {step_id!r} is {last['status']} (task #{last['id']}): nothing to retry")
    if any((latest.get(d) or {}).get("status") != "done" for d in step["needs"]):
        raise WorkflowError(f"step {step_id!r} waits on {step['needs']}, which have not all passed")
    task = _queue_step(store, run, defn, step, json.loads(run["inputs"]), json.loads(run["options"]), latest)
    if last is not None:
        # The try that stopped paused the lane; a retry is the person saying go on.
        store.release_lane(run["repo"], last["id"])
    (kick or _kick)(run["cwd"], run["repo"])
    return task
