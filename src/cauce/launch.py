"""One task attempt, run as a one-shot `claude -p` worker.

A one-shot worker instead of a subagent, for four reasons a subagent cannot
give:

- **Its tools are enforced, not requested.** `--tools`, `--disallowedTools`
  and `--strict-mcp-config` decide what exists in the worker. A subagent told
  "you may not edit" in its prompt still has the edit tool.
- **Its model and effort are per attempt.** Escalation needs a different cell
  on every retry; a subagent's model is fixed in its charter.
- **Its context is its own and dies with it.** A debugging session's dead ends,
  logs and tool output never reach the orchestrator. What comes back is the
  result block, nothing else.
- **It runs where it is told.** It is launched from one directory (whose
  settings and instructions it loads) and works in another (`--add-dir`).

Three things it never takes from the model. Changed files come from git, not
the worker's report — the party being checked does not supply the evidence. A
missing or ambiguous verdict is *inconclusive*, never a pass. And what the
permission settings refused comes from the CLI's own `permission_denials`: a
worker that could not run `npm run build` is blocked on a setting, whatever it
says about the code.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cauce import allow, grants, models
from cauce.escalate import REPLAN, Failure
from cauce.matrix import Cell

RESULT_FENCE = "cauce-result"
_FENCE_RE = re.compile(rf"```{RESULT_FENCE}\s*\n(?P<body>.*?)\n?```", re.DOTALL | re.IGNORECASE)

DEFAULT_MAX_TURNS = 30
DEFAULT_TIMEOUT_S = 1800

#: Tools a reading task never needs. Removing them is what makes "read-only"
#: true instead of requested.
WRITE_TOOLS: tuple[str, ...] = ("Edit", "Write", "NotebookEdit", "MultiEdit")

RESULT_INSTRUCTIONS = f"""
When you are done, end your reply with exactly one fenced block:

```{RESULT_FENCE}
{{
  "verdict": "pass" | "fail" | "inconclusive",
  "failure": "code_bug" | "test_bug" | "approach" | "spec_bug" | "architecture_bug" | "environment" | null,
  "summary": "one or two sentences: what you did, or what stopped you",
  "evidence": "the command you ran to check it and its real output, verbatim",
  "learned": ["a durable fact about this project that you confirmed and the next person would want"]
}}
```

- `pass` means the task is done and you can show it. If you could not run what
  would show it, the verdict is `inconclusive`, not `pass`.
- If the task contradicts itself or its check, or cannot be done as written,
  the verdict is `fail` with `spec_bug` — say what is contradictory. Do not
  bend the work to make a check pass.
- `evidence` is terminal output, not a description of it. A pass without it is
  refused.
- On a failure, `failure` says where the problem lives: `code_bug` (you left a
  bug or missed a case), `test_bug` (a test you relied on is wrong), `approach`
  (the direction was wrong), `spec_bug` (the task as written cannot be done),
  `architecture_bug` (the design prevents it), `environment` (a tool or service
  was missing or down).
- `summary` says what the files now show, nothing more. Never describe a change
  you did not make or could not save; if you changed nothing, say so.
- If a command you needed was refused, name it in `summary`; the verdict is
  `inconclusive`. The work you did stays: it is kept for a person to see.
- Nobody can approve a command while you run: what your settings do not allow
  is refused. Read and search with the Read, Grep and Glob tools, which need no
  approval, rather than with shell commands. Run one shell command per call
  instead of chaining them with `;`, `&&` or `|`: every part of a chain is
  checked, and one refused part refuses all of it.
- Run a project's tools by name (`npm test`, `npx tsc`), as its scripts do. Do
  not source a version manager or call a binary by its full path: the
  project's own Node, when it declares one, is already first on your PATH, and
  if the version is still wrong, answer `fail` with `environment` and say so.
- A command that never returns (a dev server, a watcher) blocks the call it
  runs in: start it in the background and stop it before you finish. You have
  no browser unless your tools include one; without it, answer `fail` with
  `environment` and say so, never describe a page you did not open.
- Do not list the files you changed; they are read from git.
- `learned` goes into the project's notes, read by whoever works here next. Put
  at most five facts you confirmed while working that outlive this task: a
  business rule, why the code is shaped this way, a convention, how to run or
  test something, a trap. Not what you changed, not guesses, not anything
  plain from reading the code, never a secret. `[]` when there is none.
""".strip()

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class LaunchSpec:
    prompt: str
    cell: Cell
    #: Where the worker starts. Its project settings and instructions load from
    #: here, and it is the directory its changes are read from unless `workdir`
    #: says otherwise.
    launch_dir: Path
    #: Where the work happens, when that is not the launch directory.
    workdir: Path | None = None
    #: Directories outside it the worker may read: the checkout's dependency
    #: folders a worktree links to, which otherwise resolve outside its reach.
    extra_dirs: tuple[Path, ...] = ()
    #: What `--model` gets, when a person pinned the cell's alias to a model id.
    model_id: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    max_budget_usd: float | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    #: None leaves the built-in tool set alone; a tuple names the only ones.
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    #: Permission rules a person granted for this task, e.g. `Bash(npm run build)`:
    #: without them a one-shot worker is refused anything its settings do not allow.
    allowed_tools: tuple[str, ...] = ()
    #: The MCP servers this worker gets, as an `mcpServers` mapping. Always
    #: strict: a worker sees only what its task was given.
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    permission_mode: str = "acceptEdits"
    append_system_prompt: str = ""
    persist_session: bool = False
    #: Extra environment for the worker: which task and attempt it is, so its
    #: tool calls are recorded against them.
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def target_dir(self) -> Path:
        return self.workdir or self.launch_dir


@dataclass(frozen=True)
class WorkerResult:
    passed: bool
    failure: Failure | None
    summary: str
    evidence: str = ""
    changed_paths: tuple[str, ...] = ()
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    duration_s: float = 0.0
    raw: str = ""
    #: What the worker's own block said: pass, fail, inconclusive, or "" for none.
    verdict: str = ""
    #: The tool calls the permission settings refused, from the CLI, not the worker.
    denied: tuple[str, ...] = ()
    #: The `--allow` rules that would let them through, from the full calls.
    allow: tuple[str, ...] = ()
    #: The model the CLI says did the work, e.g. `claude-sonnet-5-5`; "" when unknown.
    served_model: str = ""
    #: The permission mode the worker ran in, after any fallback.
    permission_mode: str = ""
    #: Facts about the project the worker says it confirmed: filed in its notes on a pass.
    learned: tuple[str, ...] = ()
    #: Refused writes to files the attempt changed anyway, per git: no block.
    overcome: tuple[str, ...] = ()
    #: The launch broke (a timeout, no JSON, an errored session), not the work:
    #: the one failure a retry can help.
    transient: bool = False


def build_argv(spec: LaunchSpec, *, claude_bin: str = "claude", mcp_config_path: Path | None = None) -> list[str]:
    argv = [
        claude_bin, "-p",
        "--model", spec.model_id or spec.cell.model,
        "--output-format", "json",
        "--max-turns", str(spec.max_turns),
        "--permission-mode", spec.permission_mode,
        "--strict-mcp-config",
    ]
    if spec.cell.effort:
        argv += ["--effort", spec.cell.effort]
    if spec.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{spec.max_budget_usd:.2f}"]
    if spec.tools is not None:
        argv += ["--tools", ",".join(spec.tools)]
    if spec.disallowed_tools:
        argv += ["--disallowedTools", ",".join(spec.disallowed_tools)]
    if spec.allowed_tools:
        argv += ["--allowedTools", *spec.allowed_tools]
    if spec.workdir is not None and spec.workdir != spec.launch_dir:
        argv += ["--add-dir", str(spec.workdir)]
    for extra in spec.extra_dirs:
        argv += ["--add-dir", str(extra)]
    if mcp_config_path is not None:
        argv += ["--mcp-config", str(mcp_config_path)]
    if spec.append_system_prompt:
        argv += ["--append-system-prompt", spec.append_system_prompt]
    if not spec.persist_session:
        argv.append("--no-session-persistence")
    return argv


def run(
    spec: LaunchSpec,
    *,
    runner: Runner = subprocess.run,
    claude_bin: str = "claude",
) -> WorkerResult:
    """Run one attempt. Raises only when the CLI itself cannot start."""
    mcp_path = None
    if spec.mcp_servers:
        # Outside the repository: a config file the worker could commit is a
        # config file that ends up in someone's history.
        fd, name = tempfile.mkstemp(prefix="cauce-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": dict(spec.mcp_servers)}, fh)
        mcp_path = Path(name)
    argv = build_argv(spec, claude_bin=claude_bin, mcp_config_path=mcp_path)
    before = tracked_state(spec.target_dir)
    started = time.monotonic()
    try:
        proc = runner(
            argv,
            input=spec.prompt + "\n\n" + RESULT_INSTRUCTIONS,
            cwd=str(spec.launch_dir),
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            env={**worker_env(), **spec.env},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WorkerResult(
            False, Failure.ENVIRONMENT, f"the worker exceeded its {spec.timeout_s}s timeout",
            duration_s=float(spec.timeout_s), transient=True,
        )
    except FileNotFoundError as exc:
        raise LaunchError(f"{claude_bin!r} is not on PATH: a worker that cannot start is a "
                          "configuration problem, not a failed task") from exc
    finally:
        if mcp_path is not None:
            mcp_path.unlink(missing_ok=True)
    if _mode_refused(proc, spec):
        # This CLI does not take the mode (an older one, or auto mode not on for
        # the account): the attempt runs again, as it would have before.
        return run(dataclasses.replace(spec, permission_mode=grants.FALLBACK_MODE), runner=runner,
                   claude_bin=claude_bin)
    changed = changed_paths(before, tracked_state(spec.target_dir), spec.target_dir)
    return dataclasses.replace(parse(proc, spec, changed, duration_s=time.monotonic() - started),
                               permission_mode=spec.permission_mode)


def _mode_refused(proc: subprocess.CompletedProcess, spec: LaunchSpec) -> bool:
    if spec.permission_mode == grants.FALLBACK_MODE or proc.returncode == 0 or _json(proc.stdout):
        return False
    said = (proc.stderr or "").lower()
    return "permission-mode" in said or "permission mode" in said or "auto mode" in said


class LaunchError(Exception):
    """The CLI is missing or unusable. Fails loudly: escalating it would spend the
    whole ladder proving `claude` is still not installed."""


def parse(
    proc: subprocess.CompletedProcess,
    spec: LaunchSpec,
    changed: tuple[str, ...] = (),
    *,
    duration_s: float = 0.0,
) -> WorkerResult:
    envelope = _json(proc.stdout)
    usage = _usage(envelope)
    envelope, overcome = _overcome(envelope, changed, spec.target_dir)
    denied = denials(envelope)
    common = dict(changed_paths=changed, duration_s=duration_s, denied=denied, allow=allow_rules(envelope),
                  overcome=overcome, **usage)
    if envelope is None:
        detail = (proc.stderr or "").strip()[:300]
        return WorkerResult(False, Failure.ENVIRONMENT,
                            f"the CLI produced no JSON (exit {proc.returncode}): {detail}", transient=True, **common)
    text = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        subtype = str(envelope.get("subtype") or "")
        if "max_turns" in subtype:
            return WorkerResult(False, Failure.TURNS_EXHAUSTED,
                                f"used all {spec.max_turns} turns without a verdict", raw=text, **common)
        if "budget" in subtype:
            return WorkerResult(False, Failure.BUDGET_EXHAUSTED,
                                f"reached its ${spec.max_budget_usd} cap without a verdict", raw=text, **common)
        # An API error (a usage limit, an overload) arrives as `is_error` with
        # subtype "success": the reason is in the result text, never the subtype.
        why = text.strip()[:200] if subtype in ("", "success") and text.strip() else subtype or "no reason given"
        return WorkerResult(False, Failure.ENVIRONMENT, f"the session errored: {why}", raw=text, transient=True,
                            **common)
    block = result_block(text)
    if block is None:
        return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                            "the worker did not answer with exactly one result block", raw=text, **common)
    verdict = str(block.get("verdict") or "").lower()
    summary = str(block.get("summary") or "")[:1000]
    evidence = str(block.get("evidence") or "")[:4000]
    common["learned"] = learned(block)
    if verdict == "pass":
        if not evidence.strip():
            return WorkerResult(False, _refused(Failure.INCONCLUSIVE, denied),
                                "claimed a pass with no evidence: " + summary, raw=text, verdict=verdict, **common)
        return WorkerResult(True, None, summary, evidence, raw=text, verdict=verdict, **common)
    failure = _refused(_failure(block.get("failure"), verdict), denied)
    return WorkerResult(False, failure, summary, evidence, raw=text, verdict=verdict, **common)


#: How many facts one attempt may add to the notes, and how long each may be.
MAX_LEARNED, LEARNED_CHARS = 5, 600


def learned(block: Mapping[str, Any]) -> tuple[str, ...]:
    """The facts a result block says the worker learned: strings only, a few, short."""
    raw = block.get("learned")
    if not isinstance(raw, list):
        return ()
    found = [" ".join(x.split())[:LEARNED_CHARS] for x in raw if isinstance(x, str) and x.strip()]
    return tuple(dict.fromkeys(found))[:MAX_LEARNED]


def _refused(failure: Failure, denied: Sequence[str]) -> Failure:
    """A refusal decides where an unfinished attempt stands, unless the worker found
    the task itself wrong: no model gets past a setting, and a stronger one sent
    into the same refusal only costs more."""
    return Failure.PERMISSION if denied and failure not in REPLAN else failure


#: The tools that write a file: a refusal of one is overcome when git shows the file written.
_WRITES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _overcome(envelope: dict[str, Any] | None, changed: Sequence[str],
              workdir: Path) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """The envelope without the refused writes the attempt got past: a write to a
    file in its own work directory that git shows changed after all (a retry, an
    Edit after a refused Write). Such a refusal stopped nothing, and blocking on
    it asks a person to allow what is done. The refusals dropped, as `Tool(path)`."""
    entries = (envelope or {}).get("permission_denials") or ()
    if not entries or not changed:
        return envelope, ()
    done = set(changed)
    root = workdir.resolve()
    kept, dropped = [], []
    for entry in entries:
        given = entry.get("tool_input") if isinstance(entry, dict) else None
        path = str((given or {}).get("file_path") or (given or {}).get("notebook_path") or "")
        if isinstance(entry, dict) and entry.get("tool_name") in _WRITES and path:
            full = (root / path).resolve() if not path.startswith("/") else Path(path).resolve()
            if full.is_relative_to(root) and full.relative_to(root).as_posix() in done:
                dropped.append(f"{entry['tool_name']}({full.relative_to(root).as_posix()})")
                continue
        kept.append(entry)
    if not dropped:
        return envelope, ()
    return {**(envelope or {}), "permission_denials": kept}, tuple(dict.fromkeys(dropped))


def allow_rules(envelope: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The rules a person would pass to let every refused call through: one per
    program, read from the calls in full (see `cauce.allow`)."""
    found: list[str] = []
    for entry in (envelope or {}).get("permission_denials") or ():
        if isinstance(entry, dict) and entry.get("tool_name"):
            found += allow.rules(str(entry["tool_name"]), entry.get("tool_input"))
    return tuple(dict.fromkeys(found))


def denials(envelope: Mapping[str, Any] | None) -> tuple[str, ...]:
    """What the permission settings refused, as the rules a person would allow:
    `Bash(npm run build)`, `Write(src/app.ts)`, or the bare tool name."""
    found: list[str] = []
    for entry in (envelope or {}).get("permission_denials") or ():
        if not isinstance(entry, dict) or not entry.get("tool_name"):
            continue
        name = str(entry["tool_name"])
        given = entry.get("tool_input") if isinstance(entry.get("tool_input"), dict) else {}
        target = given.get("command") or given.get("file_path") or given.get("url") or ""
        target = " ".join(str(target).split())
        rule = f"{name}({target[:117] + '...' if len(target) > 120 else target})" if target else name
        if rule not in found:
            found.append(rule)
    return tuple(found)


def result_block(text: str) -> dict[str, Any] | None:
    """The one result block, or None — two verdicts are no verdict."""
    matches = _FENCE_RE.findall(text or "")
    if len(matches) != 1:
        return None
    try:
        parsed = json.loads(matches[0])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: Where a failure lives is worth reading even from an `inconclusive` verdict: a
#: worker that found the task contradictory often says "I could not finish"
#: rather than "I failed", and the place it names is still the place.
_DECISIVE = frozenset({Failure.SPEC_BUG, Failure.ARCHITECTURE_BUG, Failure.ENVIRONMENT})


def _failure(raw: Any, verdict: str) -> Failure:
    try:
        named = Failure(str(raw))
    except ValueError:
        named = None
    if verdict != "fail":
        return named if named in _DECISIVE else Failure.INCONCLUSIVE
    # A failure that says nothing about where it lives is treated as shallow:
    # the cheapest move that still changes something.
    return named or Failure.CODE_BUG


def _json(stdout: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout or "")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _usage(envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    if not envelope:
        return {}
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    cost = envelope.get("total_cost_usd")
    return {
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else 0.0,
        "input_tokens": _int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
        + _int(usage.get("cache_creation_input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "turns": _int(envelope.get("num_turns")),
        "served_model": models.served(envelope),
    }


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CAUCE_HOOKS_OFF"] = "1"
    env.pop("CAUCE_SESSION_ID", None)  # the worker's work is the task's, not that session's turn
    return env


# --- what changed ---------------------------------------------------------


#: How deep below the work directory a nested git checkout is looked for, and how
#: many are read: a folder holding a few repositories, not a whole disk.
NESTED_DEPTH, NESTED_MAX = 2, 24
_SKIP_DIRS = frozenset({"node_modules", ".venv", "venv", ".git", "dist", "build", "target", "__pycache__"})


def tracked_state(directory: Path) -> dict[str, str] | None:
    """`git status` of a directory, path -> status code, and of every git checkout
    nested in it, their paths prefixed. A folder that holds several repositories
    is no checkout itself, and a checkout's status does not look inside a nested
    one: without these, work done in them reads as "changed nothing".
    None when neither the directory nor anything in it is under git."""
    own = _status(directory)
    nested = nested_checkouts(directory)
    if own is None and not nested:
        return None
    state = dict(own or {})
    for sub in nested:
        found = _status(sub)
        if found is None:
            continue
        rel = sub.relative_to(directory).as_posix()
        for path, code in found.items():
            state[f"\0HEAD:{rel}" if path == "\0HEAD" else f"{rel}/{path}"] = code
    # The parent's own status lists a nested checkout as one untracked folder.
    for sub in nested:
        state.pop(sub.relative_to(directory).as_posix() + "/", None)
    return state


def _status(directory: Path) -> dict[str, str] | None:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "-z"],
            cwd=str(directory), capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    state: dict[str, str] = {}
    for entry in proc.stdout.split("\0"):
        if len(entry) > 3:
            state[entry[3:]] = entry[:2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(directory),
                          capture_output=True, text=True, check=False)
    state["\0HEAD"] = head.stdout.strip()
    return state


def nested_checkouts(directory: Path) -> list[Path]:
    """Git checkouts below `directory`, at most `NESTED_DEPTH` levels down."""
    found: list[Path] = []
    level = [directory]
    for _ in range(NESTED_DEPTH):
        below: list[Path] = []
        for parent in level:
            try:
                children = sorted(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink())
            except OSError:
                continue
            for child in children:
                if child.name in _SKIP_DIRS or child.name.startswith("."):
                    continue
                if (child / ".git").exists():
                    found.append(child)
                    if len(found) >= NESTED_MAX:
                        return found
                else:
                    below.append(child)
        level = below
    return found


def changed_paths(
    before: dict[str, str] | None, after: dict[str, str] | None, directory: Path | None = None
) -> tuple[str, ...]:
    if before is None or after is None:
        return ()
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p) and not p.startswith("\0HEAD")}
    if directory is not None:
        for key in {k for k in set(before) | set(after) if k.startswith("\0HEAD")}:
            old, new = before.get(key), after.get(key)
            if not (old and new and old != new):
                continue
            # The worker committed: what it committed is clean in `status` and would
            # otherwise read as "changed nothing".
            rel = key.removeprefix("\0HEAD").removeprefix(":")
            diff = subprocess.run(["git", "diff", "--name-only", old, new], cwd=str(directory / rel),
                                  capture_output=True, text=True, check=False)
            changed |= {f"{rel}/{line}" if rel else line for line in diff.stdout.splitlines() if line}
    return tuple(sorted(changed))


def describe(argv: Sequence[str]) -> str:
    """The command line with the prompt-sized arguments shortened, for logs."""
    shown = []
    for arg in argv:
        shown.append(arg if len(arg) <= 80 else arg[:77] + "...")
    return " ".join(shown)
