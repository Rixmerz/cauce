"""Habits: repeated tool sequences, mined, and turned into automation only with a person's say.

The idea is muscle-memory's; the code is cauce's. Every tool call is recorded
as a **signature** — the verb and the kind of thing it acted on, `bash:pytest`,
`edit:.py` — plus a hash of its arguments. Never the arguments, the file
contents, the output or the prompt: the miner needs to know *that* the
formatter ran after a Python edit, not what was in either. A last hard filter
keeps the signature to `[a-z0-9:._-]`, so a case the scrubbing missed still
cannot carry a path or a secret.

From those records:

- **candidates** — sequences that repeat *and could run on their own*: they
  start with an edit or a write and end in a command that does something (a
  formatter, a linter, tests, a build). The model looking around — `find → ls`,
  `cat → cd` — repeats in every session and is no habit: a hook could not take
  it over or save a turn. Gates are boolean (automatable, enough occurrences,
  in enough sessions, nearly always succeeding, nothing destructive); the score
  only ranks what passed them. A large count must never buy its way past a gate.
- **recipes** — per kind of task, the sequences that came before a worker's
  pass. They go into the next worker's brief: the steps that worked here last
  time, so it does not spend turns rediscovering them.
- **automation** — a candidate becomes a `PostToolUse` hook only when a person
  runs `cauce habits install` with the command to run. Nothing in cauce
  installs one by itself. An installed habit that fails three times in a row
  turns itself off, and `cauce habits uninstall` removes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cauce import signature as sig
from cauce.signature import arg_hash, signature  # noqa: F401 - re-exported for callers and tests
from cauce.store import Store
from cauce.text import clip

#: The steps a hook can be triggered by: an edit or a write of a file type.
_EDIT_TRIGGER = re.compile(r"^(edit|write):(.+)$")
#: Signatures a candidate may never contain: automation must not destroy.
_DESTRUCTIVE = re.compile(r"bash:(rm|git-push|git-reset|git-clean|git-checkout|docker-rm|kubectl-delete|"
                          r"terraform-apply|terraform-destroy|dd|mkfs|shutdown|reboot)")

#: Bash programs that look, move around or print: a sequence of these is the
#: model finding its way, never a step worth automating. `find → ls` repeating
#: in every session is how exploring looks, not a habit a hook could take over.
_LOOKING = frozenset({
    "ls", "find", "fd", "cat", "bat", "head", "tail", "less", "more", "cd", "pushd", "popd", "pwd", "echo",
    "printf", "grep", "rg", "ag", "egrep", "wc", "tree", "which", "type", "file", "stat", "du", "df", "sort",
    "uniq", "cut", "tr", "diff", "sed", "awk", "jq", "xargs", "sleep", "date", "env", "export", "true", "test",
    "mkdir", "touch", "cp", "mv", "ln", "chmod", "realpath", "dirname", "basename", "readlink", "unknown",
    "git", "git-status", "git-log", "git-diff", "git-show", "git-branch", "git-remote", "git-rev-parse",
    "git-ls-files", "git-blame", "git-config", "git-stash", "git-fetch", "git-add",
})

MIN_OCCURRENCES = 3
MIN_SESSIONS = 2
MIN_SUCCESS = 0.9
MAX_LENGTH = 4
MAX_FAILURES = 3

def load_events(store: Store, env: Mapping[str, str] | None = None) -> int:
    """Move new lines from the hooks' append-only logs into the database."""
    directory = sig.log_dir(dict(os.environ) if env is None else env)
    if not directory.is_dir():
        return 0
    count = 0
    for path in sorted(directory.glob("*.ndjson")):
        offset = store.transcript_offset(str(path))
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    record = json.loads(raw)
                    store.add_tool_event(**{k: record.get(k) for k in (
                        "ts", "session_id", "task_id", "attempt", "tool", "sig", "arg_hash", "ok")})
                    count += 1
                except (ValueError, TypeError):
                    continue
        store.set_transcript_offset(str(path), offset)
    return count


# --- mining -----------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    steps: tuple[str, ...]
    occurrences: int
    sessions: int
    success: float
    distinct_args: int
    score: float
    #: The repositories it was seen in.
    repos: int = 1

    @property
    def id(self) -> str:
        return hashlib.sha256(" > ".join(self.steps).encode()).hexdigest()[:8]

    def line(self) -> str:
        where = f", {self.repos} repositories" if self.repos > 1 else ""
        return (f"{self.id}  {' → '.join(self.steps)}  ×{self.occurrences} in {self.sessions} sessions{where}, "
                f"{self.success:.0%} ok")

    def data(self) -> dict[str, Any]:
        return {"id": self.id, "steps": list(self.steps), "occurrences": self.occurrences,
                "sessions": self.sessions, "repos": self.repos, "success": self.success, "score": self.score}


def _runs(events: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for e in events:
        by_run[f"{e['session_id']}:{e['task_id']}:{e['attempt']}"].append(e)
    return by_run


def _unit(e: Mapping[str, Any]) -> str:
    """What counts as one independent place a sequence was seen: a worker's task (its events
    carry the session that sent it, so every worker of one session would be one place), else
    the session."""
    return f"task:{e['task_id']}" if e.get("task_id") else str(e.get("session_id"))


def _step(call: str) -> str:
    """A write is an edit to a hook: its matcher takes `Edit|Write|MultiEdit`, so `write:.py →
    pytest` and `edit:.py → pytest` are one habit, never two offered side by side."""
    return "edit:" + call[len("write:"):] if call.startswith("write:") else call


def _project(repo: object) -> str | None:
    """The repository an event belongs to, or None when it is no project: the home directory,
    the root, a temporary directory (a benchmark's, a test's), Claude Code's or cauce's own
    folders. Their sequences are kept out of every count of repositories."""
    if not isinstance(repo, str) or not repo:
        return None
    if not repo.startswith("/"):
        return repo
    import tempfile

    from cauce import project

    path = Path(repo)
    temp = Path(tempfile.gettempdir()).resolve()
    if project._never(path) or path == temp or temp in path.parents or project._tools_home(path):
        return None
    return repo


def candidates(events: Sequence[Mapping[str, Any]]) -> list[Candidate]:
    """Sequences of 2–4 consecutive signatures that pass every gate, best first."""
    occurrences: dict[tuple[str, ...], list[tuple[str, bool, str, Any]]] = defaultdict(list)
    for run in _runs(events).values():
        session = _unit(run[0])
        sigs = [_step(e["sig"]) for e in run]
        for length in range(2, MAX_LENGTH + 1):
            for i in range(len(sigs) - length + 1):
                steps = tuple(sigs[i:i + length])
                if len(set(steps)) == 1:
                    continue  # the same call repeated is a loop, not a habit
                window = run[i:i + length]
                occurrences[steps].append((session, all(e["ok"] for e in window),
                                           "".join(e["arg_hash"] for e in window), _project(window[0].get("repo"))))
    found = []
    for steps, seen in occurrences.items():
        if not automatable(steps):
            continue
        n = len(seen)
        sessions = len({s for s, _, _, _ in seen})
        success = sum(ok for _, ok, _, _ in seen) / n
        gates = (n >= MIN_OCCURRENCES and sessions >= MIN_SESSIONS and success >= MIN_SUCCESS
                 and not any(_DESTRUCTIVE.match(step) for step in steps))
        if not gates:
            continue
        distinct = len({h for _, _, h, _ in seen})
        repos = len({r for _, _, _, r in seen if r}) or 1
        found.append(Candidate(steps, n, sessions, success, distinct, score=n * sessions * success, repos=repos))
    # A hook runs on the edit that starts a sequence and runs the command that ends it: what
    # came between changes nothing it does. Sequences with the same edit and the same command
    # are one habit, its shortest form; a longer one that repeats as often as its prefix is
    # the same habit, better described.
    found.sort(key=lambda c: (len(c.steps), -c.score))
    ends: dict[tuple[str, str], Candidate] = {}
    for c in found:
        ends.setdefault((c.steps[0], c.steps[-1]), c)
    found = sorted(ends.values(), key=lambda c: (-c.score, -len(c.steps)))
    kept: list[Candidate] = []
    for c in found:
        if any(_contains(k.steps, c.steps) and k.occurrences >= c.occurrences for k in kept):
            continue
        kept.append(c)
    return kept


#: An interpreter named alone ran a script or a `-c` one-off: `python -m pytest`
#: is recorded as `bash:pytest`, so a bare `bash:python3` is never a check a hook
#: could name. After a `.ts` write it was the model's own scratch work, 12 times.
_SCRIPTS = frozenset({"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "osascript"})

#: A habit seen in this many repositories is offered to one that has no history
#: of it yet: a team's way of working, not one checkout's.
MIN_REPOS_ELSEWHERE = 2


#: The files that say a repository has a command's tool. A habit of other repositories is
#: offered to one only when they are there: `deno test` to a Python project is noise.
TOOL_MARKERS: dict[str, tuple[str, ...]] = {
    "pytest": ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini"),
    "ruff": ("pyproject.toml", "ruff.toml", ".ruff.toml"), "mypy": ("pyproject.toml", "mypy.ini", "setup.cfg"),
    "black": ("pyproject.toml",), "deno": ("deno.json", "deno.jsonc"), "cargo": ("Cargo.toml",),
    "go": ("go.mod",), "npm": ("package.json",), "pnpm": ("package.json",), "yarn": ("package.json",),
    "npx": ("package.json",), "bun": ("package.json",), "jest": ("package.json",), "vitest": ("package.json",),
    "eslint": ("package.json",), "prettier": ("package.json",), "tsc": ("package.json", "tsconfig.json"),
    "make": ("Makefile",), "gradle": ("build.gradle", "build.gradle.kts"), "mvn": ("pom.xml",),
}


def checkout_of(cwd: str | None) -> Path | None:
    """The top of the checkout a directory is in, where a tool's marker files live; None when
    it is gone (a removed worktree) or in none."""
    if not cwd:
        return None
    here = Path(cwd)
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d
    return here if here.is_dir() else None


def has_tool(repo_dir: Path | None, steps: Sequence[str]) -> bool:
    """Whether a repository has the tool a habit's command runs: its program's marker files
    (`TOOL_MARKERS`, by the program's first word, `deno-test` → `deno`). A program cauce
    knows no marker for is not offered from elsewhere: there is no evidence it is here."""
    if repo_dir is None:
        return False
    program = steps[-1].removeprefix("bash:")
    markers = TOOL_MARKERS.get(program) or TOOL_MARKERS.get(program.split("-", 1)[0])
    return bool(markers) and any((repo_dir / m).is_file() for m in markers)


def for_repo(events: Sequence[Mapping[str, Any]], repo: str,
             repo_dir: Path | None = None) -> tuple[list[Candidate], list[Candidate]]:
    """A repository's habits, mined from its own events and gated on them alone,
    and those other repositories share that it does not have yet and whose tool it has
    (`has_tool`). A habit is per project: its command is this project's tooling, and a
    count summed over unrelated projects says nothing about this one."""
    here = candidates([e for e in events if e.get("repo") == repo])
    mine = [c.steps for c in here]
    elsewhere = [c for c in candidates([e for e in events if _project(e.get("repo"))])
                 if c.repos >= MIN_REPOS_ELSEWHERE and has_tool(repo_dir, c.steps)
                 and not any(c.steps == m or _contains(m, c.steps) or _contains(c.steps, m) for m in mine)]
    return here, elsewhere


def automatable(steps: Sequence[str]) -> bool:
    """Whether a hook could take a sequence over: it starts with an edit or a write
    (what a hook is triggered by) and ends in a command that does something — a
    formatter, a linter, a test run, a build. Anything else is a false positive:
    it repeats, but nothing about it can run on its own or save a turn."""
    if len(steps) < 2 or _EDIT_TRIGGER.match(steps[0]) is None:
        return False
    last = steps[-1]
    program = last.removeprefix("bash:")
    return (last.startswith("bash:") and program not in _LOOKING and program not in _SCRIPTS
            and checks_its_edit(steps))


#: The file types a program checks, by its first word. A habit whose command does not check
#: the file its edit wrote (`edit:.md → ruff-check`) is the session's routine after any
#: change, not something that edit causes: a hook on it would run the linter on every note.
#: A program not listed (`make`, a project's own script) may check anything.
ECOSYSTEMS: dict[str, frozenset[str]] = {
    **dict.fromkeys(("pytest", "ruff", "mypy", "black", "isort", "flake8", "pylint", "pyright", "coverage", "tox",
                     "uv", "poetry"), frozenset({".py", ".pyi", ".toml", ".cfg", ".ini"})),
    **dict.fromkeys(("deno", "npm", "npx", "pnpm", "yarn", "bun", "jest", "vitest", "eslint", "tsc", "node"),
                    frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".json", ".jsonc", ".css",
                               ".vue", ".svelte", ".html"})),
    "prettier": frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".css", ".scss", ".html", ".md", ".yaml",
                           ".yml", ".vue", ".svelte"}),
    "cargo": frozenset({".rs", ".toml"}), "go": frozenset({".go", ".mod"}), "gofmt": frozenset({".go"}),
    "mvn": frozenset({".java", ".xml", ".kt"}), "gradle": frozenset({".java", ".kt", ".kts", ".gradle"}),
    "rubocop": frozenset({".rb"}), "rspec": frozenset({".rb"}), "bundle": frozenset({".rb", ".gemspec"}),
    "markdownlint": frozenset({".md"}), "shellcheck": frozenset({".sh", ".bash"}),
}


def checks_its_edit(steps: Sequence[str]) -> bool:
    """Whether a habit's command checks the kind of file its edit wrote (`ECOSYSTEMS`)."""
    trigger = _EDIT_TRIGGER.match(steps[0])
    program = steps[-1].removeprefix("bash:")
    types = ECOSYSTEMS.get(program) or ECOSYSTEMS.get(program.split("-", 1)[0])
    return trigger is None or types is None or trigger.group(2) in types


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    n = len(shorter)
    return len(longer) > n and any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


# --- review, by a worker ------------------------------------------------------------

#: The environment variable a habit's command reads the edited file from.
EDITED_VAR = "CAUCE_EDITED"
#: Meta key per repository: the candidates a review was already queued for.
REVIEWED_PREFIX = "habits.reviewed:"
#: The days of events a review is mined from.
REVIEW_DAYS = 30
#: What a review's worker may take at most: the hook stops a command at 110 s.
REVIEW_SECONDS = 30


def unreviewed(store: Store, repo: str, repo_dir: Path | None, *, days: int = REVIEW_DAYS) -> list[Candidate]:
    """The habits found in a repository that no review was queued for and none installed:
    its own, and those of other repositories whose tool it has."""
    load_events(store)
    here, elsewhere = for_repo(store.tool_events(days=days), repo, repo_dir)
    done = set(json.loads(store.meta(REVIEWED_PREFIX + repo) or "[]"))
    installed = {h["candidate"] for h in store.habits() if not h["removed"]
                 and repo_dir is not None and Path(h["settings_path"]).parent.parent == repo_dir}
    return [c for c in [*here, *elsewhere] if c.id not in done and c.id not in installed]


def review_brief(found: Sequence[Candidate]) -> str:
    """A read-only worker's request: one criterion per habit, each answered with whether it is
    worth a hook here and, if so, the exact command, run and timed."""
    lines = "\n".join(f"- {c.id}: after {c.steps[0]}, the session ran {' → '.join(c.steps[1:])} "
                       f"({c.occurrences} times in {c.sessions} sessions)" for c in found)
    return (
        "Review these habits cauce found in this repository's tool calls, each an edit followed by the same "
        "command, as candidates for a hook that runs a command after every such edit. Change no file. "
        "Answer each one below as its own criterion:\n"
        f"{lines}\n\n"
        "For each, find whether this repository has the tool and how it is run here (its scripts in "
        "deno.json, package.json, pyproject.toml or a Makefile), and pick one command for the hook: scoped to "
        f"the edited file through \"${EDITED_VAR}\" (its path) when the tool takes a file (a linter, a "
        "formatter's check, the tests next to it), else the narrowest check. Run it once on a real file of "
        f"that type and time it: over {REVIEW_SECONDS} seconds is too slow for a hook. Skip one too slow, one "
        "that checks far more than the edit, or one the session ran out of routine rather than because of "
        f"that edit. The command is one line under {REVIEW_COMMAND_CHARS} characters: a hook that needs a "
        "script is a skip. Each criterion's evidence starts with its verdict, exactly "
        f"`install <id>: `<command>`` (the command in backticks, as you ran it) or `skip <id>: <why>`, then "
        "its wall time and what you found.")


#: A habits review's verdict at the start of a criterion's evidence (`review_brief`): a command
#: whose closing backtick is missing was cut where the evidence was kept.
VERDICT = re.compile(r"\b(install|skip) ([0-9a-f]{8}):\s*(?:`([^`]+)`|(`?[^\n]+))")
#: The longest command a review may propose: a hook's one line, read whole from a verdict.
REVIEW_COMMAND_CHARS = 160
#: How much of a skip's reason an ending carries.
SKIP_CHARS = 140


def review_verdicts(texts: Sequence[str], reviewed: Sequence[str]) -> tuple[dict[str, str], dict[str, str]]:
    """The commands to install and the reasons to skip a review gave, by habit id: only the ids
    it was asked about, the first verdict for each."""
    installs: dict[str, str] = {}
    skips: dict[str, str] = {}
    for text in texts:
        for verb, cid, command, why in VERDICT.findall(text or ""):
            if cid not in reviewed or cid in installs or cid in skips:
                continue
            if verb == "install" and command.strip():
                installs[cid] = command.strip()
            elif verb == "install":
                skips[cid] = "its command was cut or not given in backticks (`cauce show` has the worker's words)"
            else:
                skips[cid] = clip((command or why).strip(), SKIP_CHARS)
    return installs, skips


def queue_review(store: Store, *, repo: str, cwd: str, session_id: str | None,
                 kick: Any = None) -> dict | None:
    """Queue one read-only worker that reviews a repository's new habits, once per habit. Its
    ending reaches the session with the install lines; installing stays the person's."""
    top = checkout_of(cwd)
    found = unreviewed(store, repo, top)
    if not found or top is None:
        return None
    with store.immediate():
        done = json.loads(store.meta(REVIEWED_PREFIX + repo) or "[]")
        found = [c for c in found if c.id not in done]
        if not found:
            return None
        store.set_meta(REVIEWED_PREFIX + repo, json.dumps([*done, *(c.id for c in found)]))
        task = store.create_task(review_brief(found), status="queued", source="queue", author="habits",
                                 repo=repo, cwd=str(top), session_id=session_id,
                                 title=f"[habits review] {len(found)} habit(s): "
                                       + ", ".join(c.id for c in found)[:150],
                                 options=json.dumps({"kind": "explore", "keep_learned": False,
                                                     "habit_review": [c.id for c in found]}))
    if kick is not None:
        kick(str(top), repo)
    return task


def recipes(store: Store, kind: str, *, repo: str | None, limit: int = 2) -> list[str]:
    """The sequences that came before passing worker attempts of this kind, most
    common first, with nothing destructive in them."""
    passed = store.passed_attempt_events(kind, repo=repo)
    runs = _runs(passed)
    if len(runs) < MIN_OCCURRENCES:
        return []
    counts: Counter[tuple[str, ...]] = Counter()
    for run in runs.values():
        sigs = [e["sig"] for e in run if not e["sig"].startswith("read:")]
        seen = set()
        for length in (3, 2):
            for i in range(len(sigs) - length + 1):
                steps = tuple(sigs[i:i + length])
                if len(set(steps)) > 1 and steps not in seen and not any(_DESTRUCTIVE.match(s) for s in steps):
                    seen.add(steps)
        counts.update(seen)
    common = [steps for steps, n in counts.most_common() if n >= MIN_OCCURRENCES]
    out: list[str] = []
    for steps in common:
        if any(" → ".join(steps) in r for r in out):
            continue
        out.append(" → ".join(steps))
        if len(out) >= limit:
            break
    return out


# --- automation, only with a person ---------------------------------------------------




class HabitError(ValueError):
    pass


def launcher() -> Path:
    """The absolute path of `bin/cauce`: a hook in settings cannot rely on PATH."""
    return Path(__file__).resolve().parents[2] / "bin" / "cauce"


def install(store: Store, candidate: Candidate, *, command: str, repo_dir: Path) -> tuple[int, bool]:
    """Write the habit as a PostToolUse hook in the repository's local settings.
    Returns its id and whether it is new: the same command on the same file type
    in the same settings is the habit already there, never a second hook.

    Only a habit that starts with an edit can be a hook: the trigger is an edit
    of that file type, and the command is what the person says to run after it.
    """
    match = _EDIT_TRIGGER.match(candidate.steps[0])
    if match is None:
        raise HabitError("only a habit that starts with an edit or a write can run as a hook")
    settings = repo_dir / ".claude" / "settings.local.json"
    for habit in store.habits():
        if (habit["settings_path"] == str(settings) and habit["command"] == command
                and habit["trigger_suffix"] == match.group(2)):
            return int(habit["id"]), False
    data = _read_settings(settings)
    habit_id = store.add_habit(steps=list(candidate.steps), command=command, trigger_suffix=match.group(2),
                               settings_path=str(settings), candidate=candidate.id)
    entry = {"matcher": "Edit|Write|MultiEdit",
             "hooks": [{"type": "command", "command": f'"{launcher()}" habit-run {habit_id}', "timeout": 120}]}
    data.setdefault("hooks", {}).setdefault("PostToolUse", []).append(entry)
    _write_settings(settings, data)
    return habit_id, True


def uninstall(store: Store, habit_id: int) -> bool:
    habit = store.habit(habit_id)
    if habit is None:
        return False
    settings = Path(habit["settings_path"])
    data = _read_settings(settings)
    marker = f"habit-run {habit_id}"
    hooks = data.get("hooks", {}).get("PostToolUse", [])
    data.setdefault("hooks", {})["PostToolUse"] = [
        h for h in hooks if not any(marker in str(x.get("command", "")) for x in h.get("hooks", []))]
    _write_settings(settings, data)
    store.update_habit(habit_id, removed=1)
    return True


#: How much of a failed habit's output the session is shown.
OUTPUT_CHARS = 2000


def run_habit(store: Store, habit_id: int, event: Mapping[str, Any]) -> str | None:
    """The hook body. Runs the person's command after a matching edit; three
    failures in a row and the habit turns itself off. Returns what the session
    should hear: the end of a failed run's output, so the check it ran saves the
    model a turn instead of being run again by hand. None when it passed or did
    not apply."""
    habit = store.habit(habit_id)
    if habit is None or habit["disabled_at"] or habit["removed"]:
        return None
    path = str((event.get("tool_input") or {}).get("file_path") or "")
    if not path.endswith(habit["trigger_suffix"]):
        return None
    try:
        # The file the edit wrote, for a command scoped to it (`ruff check "$CAUCE_EDITED"`).
        proc = subprocess.run(habit["command"], shell=True, cwd=event.get("cwd") or None,  # noqa: S602
                              capture_output=True, text=True, timeout=110, check=False,
                              env={**os.environ, EDITED_VAR: path})
        ok, output = proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, output = False, "it ran past 110 seconds and was stopped"
    except OSError as exc:
        ok, output = False, str(exc)
    store.habit_ran(habit_id, ok=ok, max_failures=MAX_FAILURES)
    if ok:
        return None
    tail = output[-OUTPUT_CHARS:]
    now = store.habit(habit_id)
    off = (" It failed three times in a row and is off now (`cauce habits status` lists it)."
           if now and now["disabled_at"] else "")
    return f"cauce habit #{habit_id} ran `{habit['command']}` after this edit, and it failed:{off}\n{tail}"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HabitError(f"{path} is not valid JSON; fix it before installing a habit") from exc
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
