"""Before a worker is launched: would the environment stop it?

Every check here is deterministic and costs no model call. Each one turns a
stop that history shows is predictable into a finding said before any money is
spent: a check whose program is not installed, an API that does not answer, a
budget too small for one attempt, a base branch that does not exist, an input a
worktree will not have, a requirement an earlier task already stopped on.

A `blocker` stops the task as `blocked` before its first attempt, with the
command that continues it; a `warning` is printed in the run's header and the
work goes on. Nothing here writes to a repository. The one thing it keeps is a
requirement a task stopped on (`learn`): a note under `environment` with a
probe cauce ran itself and saw fail, never the worker's word alone, and a
probe that passes again clears it.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import proxy_bypass_environment

from cauce import allow, runtime
from cauce.isolate import DEPENDENCY_DIRS
from cauce.matrix import MIN_ATTEMPT_USD, Cell
from cauce.text import words

BLOCKER, WARNING = "blocker", "warning"

#: How long a run waits for the API before it stops as environment, and how
#: long one connection may take.
API_WAIT_S = 180.0
API_TIMEOUT_S = 3.0
_BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
DEFAULT_API = "https://api.anthropic.com"

#: How many passing attempts of a kind and cell it takes before history, not
#: the floor in `matrix`, sizes an attempt.
MIN_COST_SAMPLES = 5

Connect = Callable[..., Any]


@dataclass(frozen=True)
class Finding:
    kind: str
    #: Which check said it: verify, api, budget, estimate, base, ignored, requirement, browser, server, login.
    check: str
    what: str
    why: str
    #: The command or step that resolves it.
    fix: str
    #: The stop cause a blocker ends the task with.
    cause: str = "preflight"
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.kind == BLOCKER

    def line(self) -> str:
        return f"{self.what}: {self.fix}" + (f" ({self.why})" if self.why else "")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "check": self.check, "what": self.what, "why": self.why, "fix": self.fix,
                "cause": self.cause, **({"data": dict(self.data)} if self.data else {})}


def check(text: str, repo_dir: Path, store: Any, *, kind: str, ladder: Sequence[Cell], start: Cell,
          options: Any, registry: Mapping[str, Any], project: str | None, env: Mapping[str, str] | None = None,
          api: bool = False, connect: Connect = socket.create_connection) -> list[Finding]:
    """Every finding for a task about to run, blockers first. `options` is the
    run's (`budget_usd`, `verify`, `isolate`, `base`). The API is probed once
    with `api`; a run waits for it instead (`wait_for_api`)."""
    env = os.environ if env is None else env
    found: list[Finding] = []
    if api and (unreachable := api_finding(env, connect)) is not None:
        found.append(unreachable)
    found += verify_findings(options.verify, repo_dir, env)
    found += budget_findings(store, kind, ladder, start, options.budget_usd, project)
    if not options.base:
        found += base_of(text, repo_dir)[1]
    if options.isolate:
        found += ignored_inputs(text, repo_dir)
    if project:
        found += requirement_findings(store, project, text, env)
    found += unmet(text, registry, store, project)
    return sorted(found, key=lambda f: f.kind != BLOCKER)


# --- the check's programs ---------------------------------------------------

#: Words a shell runs itself: no program to find.
_BUILTINS = frozenset({"test", "[", "[[", "{", "}", "!", "source", ".", "export", "unset", "exit", "cd", "eval", "exec", "command",
                       "type", "set", "true", "false", "echo", "printf", "wait", "time"})
_NPM = frozenset({"npm", "pnpm"})
_CD = re.compile(r"(?:^|[;&|(]\s*)(?:cd|pushd)\s")


def verify_findings(verify: str | None, workdir: Path, env: Mapping[str, str]) -> list[Finding]:
    """Blockers for a `--verify` command that cannot run here: a program not on
    the worker's PATH (the project's Node first, as `runtime` puts it), an npm
    script or manifest that is not there. The same walls `orchestrate.cannot_run`
    reads after the fact, found before an attempt is paid for."""
    if not verify:
        return []
    path = {**env, **runtime.env_for(workdir, env)}.get("PATH", "")
    found: list[Finding] = []
    for prefix in allow.commands(verify):
        argv = prefix.split()
        program = argv[0]
        if program in _BUILTINS:
            continue
        missing = _missing_program(program, workdir, path)
        if missing:
            found.append(Finding(BLOCKER, "verify", f"the check `{verify}` cannot run: {missing}",
                                 "a check that cannot run says nothing about the work, and every attempt "
                                 "would end on it", f"install `{program}` or fix the --verify command, "
                                                    "then run the task again"))
            continue
        if program in _NPM and not _CD.search(verify):
            found += _npm_script(verify, argv, workdir)
    return found


def _missing_program(program: str, workdir: Path, path: str) -> str | None:
    if "/" in program:
        where = Path(program) if program.startswith("/") else workdir / program
        if where.is_file() and os.access(where, os.X_OK):
            return None
        return f"{program} does not exist or cannot be run from {workdir}"
    if shutil.which(program, path=path) is None:
        return f"`{program}` is not on the worker's PATH"
    return None


def _npm_script(verify: str, argv: list[str], workdir: Path) -> list[Finding]:
    if len(argv) < 2 or argv[1].startswith("-"):
        return []
    script = argv[2] if argv[1] == "run" and len(argv) > 2 else argv[1] if argv[1] in ("test", "start") else None
    if script is None:
        return []
    manifest = workdir / "package.json"
    if not manifest.is_file():
        return [Finding(BLOCKER, "verify", f"the check `{verify}` cannot run: there is no package.json in "
                        f"{workdir}", "npm reads its scripts from it", "run the task where package.json is "
                        "(--repo), or fix the --verify command")]
    try:
        scripts = json.loads(manifest.read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return []
    if script not in scripts and script != "start":  # `npm start` without one runs server.js
        return [Finding(BLOCKER, "verify", f"the check `{verify}` cannot run: package.json has no script "
                        f"{script!r}", "npm would answer `Missing script`",
                        "add the script or fix the --verify command (scripts: "
                        f"{', '.join(sorted(scripts)) or 'none'})")]
    return []


# --- the API ------------------------------------------------------------------


def api_endpoint(env: Mapping[str, str]) -> tuple[str, int] | None:
    """Where the CLI connects: `ANTHROPIC_BASE_URL` or the API, through the proxy
    the environment names unless `NO_PROXY` exempts it. None for a cloud
    provider's endpoint, which cauce does not guess."""
    if any(env.get(k) for k in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return None
    url = urlparse(env.get("ANTHROPIC_BASE_URL") or DEFAULT_API)
    if not url.hostname:
        return None
    host, port = url.hostname, url.port or (443 if url.scheme != "http" else 80)
    keys = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy") if url.scheme != "http" else (
        "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    proxy = next((env[k] for k in keys if env.get(k)), "")
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if proxy and not (no_proxy and proxy_bypass_environment(host, {"no": no_proxy})):
        parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if parsed.hostname:
            return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _api_check_on(env: Mapping[str, str]) -> bool:
    return str(env.get("CAUCE_API_CHECK", "")).strip().lower() not in ("off", "0", "false", "no")


def reachable(host: str, port: int, connect: Connect = socket.create_connection) -> str | None:
    """Why the host cannot be reached, or None when a TCP connection opened."""
    try:
        with connect((host, port), timeout=API_TIMEOUT_S):
            return None
    except socket.gaierror as exc:
        return f"{host} does not resolve ({exc})"
    except OSError as exc:
        return f"{host}:{port} does not answer ({exc})"


def api_finding(env: Mapping[str, str], connect: Connect = socket.create_connection) -> Finding | None:
    if not _api_check_on(env) or (where := api_endpoint(env)) is None:
        return None
    why = reachable(*where, connect=connect)
    if why is None:
        return None
    return Finding(BLOCKER, "api", "the API cannot be reached from here", why,
                   "check the network, DNS or proxy, then run the task again", cause="environment",
                   data={"host": where[0], "port": where[1]})


def wait_for_api(env: Mapping[str, str] | None = None, *, wait_s: float = API_WAIT_S,
                 connect: Connect = socket.create_connection, sleep: Callable[[float], None] = time.sleep,
                 say: Callable[[str], None] | None = None,
                 cancelled: Callable[[], None] | None = None) -> Finding | None:
    """Wait for the API, re-checking with backoff for at most `wait_s`. None once it
    answers; a blocker when it never did. An unreachable API is no attempt: the
    attempt it would cost ends `ENOTFOUND` and teaches nothing."""
    env = os.environ if env is None else env
    waited, step = 0.0, 0
    while True:
        finding = api_finding(env, connect)
        if finding is None:
            return None
        if waited >= wait_s:
            return Finding(BLOCKER, "api", f"the API could not be reached for {waited:.0f}s", finding.why,
                           finding.fix, cause="environment", data=finding.data)
        pause = min(_BACKOFF_S[min(step, len(_BACKOFF_S) - 1)], wait_s - waited)
        if say is not None and step == 0:
            say(f"the API cannot be reached ({finding.why}); waiting up to {wait_s:.0f}s before stopping")
        if cancelled is not None:
            cancelled()
        sleep(pause)
        waited += pause
        step += 1


# --- budget -----------------------------------------------------------------


def dollars(amount: float) -> float:
    """An amount rounded up to the quarter dollar, as a `--budget` says it."""
    return math.ceil(round(amount * 4, 6)) / 4


def minimum_attempt(store: Any, kind: str, cell: Cell) -> tuple[float, str]:
    """The least an attempt at this cell costs before it can finish something: the
    cheapest quarter of passing attempts of this kind there, or the model's floor."""
    costs = sorted(r["cost_usd"] for r in store.passing_attempts(kind, cell=cell.label) if r["cost_usd"] > 0)
    if len(costs) >= MIN_COST_SAMPLES:
        low = costs[(len(costs) - 1) // 4]
        return low, (f"a quarter of the last {len(costs)} passing {kind} attempts at {cell.label} cost "
                     f"${low:.2f} or less")
    return MIN_ATTEMPT_USD[cell.model], f"cauce's floor for an attempt on {cell.model}"


def attempt_cost(store: Any, kind: str, cell: Cell) -> float:
    """What an attempt at this cell usually costs: the mean of passing ones, or its floor."""
    costs = [r["cost_usd"] for r in store.passing_attempts(kind, cell=cell.label) if r["cost_usd"] > 0]
    if len(costs) >= MIN_COST_SAMPLES:
        return sum(costs) / len(costs)
    return MIN_ATTEMPT_USD[cell.model]


def budget_findings(store: Any, kind: str, ladder: Sequence[Cell], start: Cell, budget: float,
                    project: str | None) -> list[Finding]:
    """A blocker when the budget cannot pay for one attempt at the start; a warning
    when it cannot pay for that attempt and one climb."""
    needed, why = minimum_attempt(store, kind, start)
    if budget < needed:
        return [budget_blocker(budget, needed, start, why, spent=0.0)]
    above = [c for c in ladder if c.sort_key() > start.sort_key()]
    if not above:
        return []
    first, climb = attempt_cost(store, kind, start), attempt_cost(store, kind, above[0])
    if budget >= first + climb:
        return []
    expected = store.expected_cost(kind, repo=project) if project else None
    expected = expected or store.expected_cost(kind)
    usual = f"; finished {kind} tasks cost ${expected[0]:.2f} on average (last {expected[1]})" if expected else ""
    return [Finding(WARNING, "estimate", f"${budget:.2f} may not cover a first attempt at {start.label} "
                    f"(about ${first:.2f}) and one climb to {above[0].label} (about ${climb:.2f})",
                    f"a task that climbs spends its budget on the cells that failed{usual}",
                    f"pass --budget {dollars(first + climb):g} to cover both",
                    data={"needed_usd": dollars(first + climb)})]


def estimate(store: Any, kind: str, ladder: Sequence[Cell], project: str | None = None) -> float | None:
    """What a task of this kind is likely to need, from history: the mean of finished
    ones, and never less than a first attempt and one climb. None without history."""
    expected = (store.expected_cost(kind, repo=project) if project else None) or store.expected_cost(kind)
    if not expected or not ladder:
        return None
    climb = attempt_cost(store, kind, ladder[0]) + (attempt_cost(store, kind, ladder[1]) if len(ladder) > 1 else 0)
    return dollars(max(expected[0], climb))


def budget_blocker(budget: float, needed: float, cell: Cell, why: str, *, spent: float) -> Finding:
    left = max(budget - spent, 0.0)
    return Finding(BLOCKER, "budget", f"the ${budget:.2f} budget has ${left:.2f} left, and an attempt at "
                   f"{cell.label} needs at least ${needed:.2f}", why,
                   f"resume with --budget {dollars(needed):g}", cause="budget",
                   data={"needed_usd": dollars(needed)})


# --- the base the task names --------------------------------------------------

_REF = r"[`'\"]?(?P<{0}>[\w][\w./-]*)[`'\"]?"
_BASE = re.compile(
    r"\b(?:from|on|off|based\s+on)\s+(?:the\s+)?branch\s+" + _REF.format("a")
    + r"|\b(?:from|off|based\s+on)\s+(?:the\s+)?" + _REF.format("b") + r"\s+branch\b"
    + r"|\b(?:at|from)\s+(?:commit\s+)?[`'\"]?(?P<sha>[0-9a-f]{7,40})\b"
    + r"|\bfrom\s+[`'\"](?P<c>[\w][\w./-]*)[`'\"]",
    re.IGNORECASE)
#: Words the patterns above catch that name no branch.
_NOT_A_REF = frozenset({"same", "current", "new", "task", "this", "that", "a", "an", "its", "their", "your",
                        "my", "own", "other", "each", "every", "one", "the", "his", "her", "our"})


def named_base(text: str) -> str | None:
    """The branch or commit the task names as its base: "from branch X", "on branch
    X", "from the X branch", "at <sha>", "from `X`". A bare "from main" is not one:
    "port the fix from main" names a source, not a base."""
    for match in _BASE.finditer(text):
        name = (match["a"] or match["b"] or match["c"] or match["sha"] or "").rstrip("./")
        if match["sha"] and not any(ch.isdigit() for ch in name):
            continue
        if len(name) >= 2 and name.lower() not in _NOT_A_REF:
            return name
    return None


def resolve(repo_dir: Path, name: str) -> str | None:
    """The commit a name resolves to here, a remote's branch of that name included."""
    for ref in (name, f"origin/{name}"):
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=str(repo_dir),
                              capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def base_of(text: str, repo_dir: Path) -> tuple[str | None, list[Finding]]:
    """The commit a worktree must start from when the task names one, and a blocker
    when it names one that does not resolve: a worktree from HEAD would be the
    wrong code, and nothing would say so."""
    name = named_base(text)
    if name is None:
        return None, []
    commit = resolve(repo_dir, name)
    if commit is not None:
        return commit, []
    return None, [Finding(BLOCKER, "base", f"the task names {name!r} as its base, and it does not resolve here",
                          "a worktree started from HEAD instead would hold other code than the task means",
                          f"fetch or create {name!r} (`git fetch`, `git branch {name} <commit>`), or rewrite the "
                          "task without it, then run it again", data={"base": name})]


# --- inputs a worktree would not have -------------------------------------------

_PATHLIKE = re.compile(r"(?<![\w/.-])((?:\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+/?|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
MAX_PATHS = 40


def ignored_inputs(text: str, repo_dir: Path) -> list[Finding]:
    """Paths the task names that exist in the checkout but not in git (ignored or
    untracked): a worktree is made from git, so the worker would not find them.
    The dependency folders a worktree links are not among them."""
    named: list[str] = []
    for raw in _PATHLIKE.findall(text):
        rel = raw.rstrip("./") if raw.endswith(("/", ".")) else raw
        if not rel or rel.startswith("/") or ".." in rel.split("/") or Path(rel).parts[0] in DEPENDENCY_DIRS:
            continue
        if (repo_dir / rel).exists() and rel not in named:
            named.append(rel)
    named = named[:MAX_PATHS]
    if not named:
        return []
    tracked = subprocess.run(["git", "ls-files", "-z", "--", *named], cwd=str(repo_dir), capture_output=True,
                             text=True, check=False)
    if tracked.returncode != 0:
        return []
    files = [f for f in tracked.stdout.split("\0") if f]
    absent = [p for p in named if not any(f == p or f.startswith(p.rstrip("/") + "/") for f in files)]
    if not absent:
        return []
    ignored = subprocess.run(["git", "check-ignore", "--", *absent], cwd=str(repo_dir), capture_output=True,
                             text=True, check=False).stdout.split("\n")
    shown = ", ".join(f"{p} ({'ignored' if p in ignored else 'untracked'})" for p in absent[:8])
    return [Finding(WARNING, "ignored", f"the task names {shown}, which a worktree will not have",
                    "a worktree is made from what git tracks", "run it with --no-isolate to work in your "
                    "checkout, or commit what the task needs", data={"paths": absent})]


# --- requirements earlier tasks stopped on ----------------------------------------

#: A note's text is one line (`notes.clean_fact`): its probe is its last words.
_PROBE = re.compile(r"\bprobe: (?P<kind>program|module|port) (?P<value>[\w.+-]+)$")
_MISSING = (
    ("program", re.compile(r"command not found:\s*([\w.+-]+)")),
    ("program", re.compile(r"\b([\w.+-]+): (?:command )?not found\b")),
    ("program", re.compile(r"`([\w.+-]+)` (?:is )?not (?:installed|found|on (?:the )?PATH)", re.IGNORECASE)),
    ("module", re.compile(r"No module named ['\"]?([A-Za-z_][\w]*)")),
    ("port", re.compile(r"(?:ECONNREFUSED|[Cc]onnection refused)[^\n]{0,40}?(?:localhost|127\.0\.0\.1):(\d{2,5})")),
    ("port", re.compile(r"(?:localhost|127\.0\.0\.1):(\d{2,5})[^\n]{0,40}?(?:ECONNREFUSED|[Cc]onnection refused)")),
)
_NOT_PROGRAMS = frozenset({"it", "this", "that", "which", "file", "module", "command", "script", "error", "zsh",
                           "bash", "sh"})
MIN_OVERLAP = 2


def missing_from(account: str) -> list[tuple[str, str]]:
    """What a worker's account says was missing, as probes: (program|module|port, value)."""
    found: list[tuple[str, str]] = []
    for kind, pattern in _MISSING:
        for value in pattern.findall(account):
            if kind == "program" and (value.lower() in _NOT_PROGRAMS or len(value) < 2):
                continue
            if kind == "port" and not 0 < int(value) < 65536:
                continue
            if (kind, value) not in found:
                found.append((kind, value))
    return found[:3]


def probe(kind: str, value: str, env: Mapping[str, str], *, connect: Connect = socket.create_connection) -> bool:
    """Whether the requirement is met now: `command -v X`, `python3 -c "import m"`, a TCP port."""
    path = env.get("PATH", "")
    if kind == "program":
        return shutil.which(value, path=path) is not None
    if kind == "module":
        python = shutil.which("python3", path=path)
        if python is None or not re.fullmatch(r"[A-Za-z_]\w*", value):
            return False
        try:
            return subprocess.run([python, "-c", f"import {value}"], capture_output=True, timeout=20,
                                  check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        with connect(("127.0.0.1", int(value)), timeout=0.5):
            return True
    except OSError:
        return False


def _how(kind: str, value: str) -> str:
    return {"program": f"command -v {value}", "module": f'python3 -c "import {value}"',
            "port": f"a server on localhost:{value}"}[kind]


def learn(store: Any, project: str, task: Mapping[str, Any], account: str, env: Mapping[str, str] | None = None,
          *, check_now: Callable[[str, str, Mapping[str, str]], bool] = probe) -> list[int]:
    """Keep what a task that stopped as `environment` was missing, as notes under
    `environment` with their probe. Only what cauce's own probe finds missing
    now is kept: the worker's word names it, cauce confirms it. The note ids."""
    from cauce import notes

    env = os.environ if env is None else env
    kept = []
    for kind, value in missing_from(account):
        if check_now(kind, value, env):
            continue
        text = (f"Tasks here need {_how(kind, value)}: task #{task['id']} stopped without it "
                f"({' '.join(str(task.get('title') or '').split())[:160]}). probe: {kind} {value}")
        note_id, _ = notes.add(store, project, text, topic="environment", author="worker", filed_by="probe",
                               title=f"needs {_how(kind, value)}", source=f"task #{task['id']}",
                               task_id=task["id"])
        kept.append(note_id)
    return kept


def requirement_findings(store: Any, project: str, text: str, env: Mapping[str, str]) -> list[Finding]:
    """The probes of requirements kept for this project whose task reads like this
    one: a blocker for each still unmet, and a met one cleared."""
    asked = set(words(text))
    found = []
    for note in store.notes(project, topics=["environment"], states=["current", "review"]):
        match = _PROBE.search(note["text"] or "")
        if match is None:
            continue
        if len(asked & set(words(note["text"][:match.start()]))) < MIN_OVERLAP:
            continue
        kind, value = match["kind"], match["value"]
        if probe(kind, value, env):
            store.update_note(note["id"], state="dropped", state_reason=f"its probe passed: {_how(kind, value)}")
            continue
        found.append(Finding(BLOCKER, "requirement", f"this repository needs {_how(kind, value)}, and it is "
                             "still missing", f"note #{note['id']}: {note['title']} (an earlier task stopped on it)",
                             f"install or start it, then run the task again; `cauce notes drop {note['id']}` if "
                             "this task does not need it", data={"note": note["id"], "probe": [kind, value]}))
    return found


# --- what a one-shot worker does not have -------------------------------------------

#: Words that say a task needs a browser, servers that keep running, or a login.
_BROWSER = re.compile(r"\b(browser|navegador|navigate|navega\w*|abr[ie]\w* (?:el )?navegador|screenshot|"
                      r"captura de pantalla|playwright|puppeteer|e2e)\b", re.IGNORECASE)
_SERVER = re.compile(r"(\bdev servers?\b|\blevanta\w*\b|\bstart (?:both |the )?(?:dev )?servers?\b|"
                     r"\bng serve\b|\bnpm (?:run )?(?:start|dev)\b|localhost:\d+)", re.IGNORECASE)
_LOGIN = re.compile(r"\b(log ?in|logged in|sign ?in|inici\w* (?:de )?sesi[oó]n|autentica\w*|authenticat\w*)\b",
                    re.IGNORECASE)
_CREDENTIALS = re.compile(r"\b(test (?:account|user)|demo (?:account|user|login)|login|credentials|"
                          r"cuenta de prueba|usuario de prueba|credenciales)\b", re.IGNORECASE)
_BROWSER_CAPS = ("browser", "playwright", "chrome", "puppeteer", "rastro")
_PORT = re.compile(r"(?:localhost|127\.0\.0\.1):(\d{2,5})\b")


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def all_listening(text: str) -> bool:
    """A task that names its local servers by port, every one of them up: the
    servers it needs are running, and saying otherwise is noise."""
    ports = {int(p) for p in _PORT.findall(text) if 0 < int(p) < 65536}
    return bool(ports) and all(listening(p) for p in ports)


def unmet(text: str, registry: Mapping[str, Any], store: Any = None, project: str | None = None) -> list[Finding]:
    """What a task asks for that a one-shot worker does not have: a browser unless
    a capability gives it one, a server it starts ends with it, and a login it
    has no account for. Warnings: the work may still get somewhere."""
    found = []
    if _BROWSER.search(text) and not any(k in name.lower() for name in registry for k in _BROWSER_CAPS):
        found.append(Finding(WARNING, "browser", "this task needs a browser and workers have none",
                             "", "register a browser MCP server in your cauce capabilities.json "
                             "(`cauce capabilities --example` has one) and allow its tools (--allow 'mcp__browser'), "
                             "or check the pages yourself"))
    if _SERVER.search(text) and not all_listening(text):
        found.append(Finding(WARNING, "server", "this task needs running servers",
                             "a server a worker starts stops when it finishes",
                             "start them yourself before dispatching, or pass the --allow rules that start them"))
    if _LOGIN.search(text) and not _credentials_known(store, project):
        found.append(Finding(WARNING, "login", "this task needs a login, and this project's notes record no test "
                             "account", "a worker cannot ask anyone for one",
                             "say in the task where it finds a test account (an environment variable, never the "
                             "secret itself), or keep that as a note with `cauce note`"))
    return found


def _credentials_known(store: Any, project: str | None) -> bool:
    if store is None or not project:
        return False
    return any(_CREDENTIALS.search(f"{n['title']}\n{n['text']}")
               for n in store.notes(project, states=["current", "review"]))
