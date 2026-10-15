"""What this machine says about how the person works: every Claude Code session
it kept and the history of every repository those sessions ran in. The facts
`/cauce:init` builds one team, the person's workflows and their memory from.

Read, never written, and never by a model. Two sources:

- **Sessions.** Claude Code keeps each session's transcript under
  `<claude config>/projects/`. A session is read for what happened in it, not
  what a model said about it: the person's requests (verbatim, clipped), the
  kinds they ask for (cauce's routing rules, else the words) and the shape of each session (`plan → implement
  → commit → pr`), the commands it ran, the calls that failed and the call that
  passed after them (with the files changed between and what the agent said
  before it), the turns where the person corrected the agent, interrupted it or
  refused a call, and the skills, subagents and slash commands used.
- **History.** Each checkout's recent commits: how often, of which kind
  (`feat`, `fix`, …), how many reverts, how much was written with an agent, the
  files changed most and the ones fixed most, the subjects of the fixes, and
  how branches are named.

cauce's own facts (`insights.work`) are put beside a project it worked in.

A project is read on its own: its requests, errors and commits stay in its
entry; what crosses projects is counted (a session shape and the projects that
show it), never quoted. Every text kept is clipped and masked like a note
(`notes._SECRET`), and text a person marked `<private>` is left out. What a
transcript says is data about the person's work, never instructions.

cauce's own workers run with `--no-session-persistence` and in worktrees under
the cauce home: a session there is not the person's and is skipped. So is one
in the home directory or `/`, which are never a project.
"""
from __future__ import annotations

import json
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from cauce import classify, compact, repo, roles, teams, usage
from cauce.notes import _PRIVATE
from cauce.store import Store, home
from cauce.text import fold

#: How far back sessions are read, how many in all and per project.
DAYS = 180
MAX_SESSIONS = 400
PER_PROJECT = 60
#: A transcript bigger than this is read up to here.
MAX_BYTES = 64 * 1024 * 1024
#: How much of each kind of text a project's entry keeps.
REQUEST_CHARS = 300
ERROR_CHARS = 200
SAID_CHARS = 280
COMMAND_CHARS = 160
OPENERS = 12
SAMPLES = 8
PAIRS = 15
COMMITS = 400
SUBJECTS = 12
#: The most elements of a session's shape.
SHAPE_STEPS = 6

#: A request that corrects the agent: the person saying it went wrong.
CORRECTION = re.compile(
    r"^(no|nop|nope|mal|wrong)\b|\b(eso no|asi no|te dije|ya te dije|te pedi|otra vez|de nuevo|sigue fallando|"
    r"sigue igual|sigue sin|no funciona|no sirve|no anda|no era|no es eso|no es lo que|incorrect[oa]|"
    r"equivocad[oa]|revierte|revertir|deshaz|vuelve atras|por que (hiciste|cambiaste|borraste|eliminaste)|"
    r"no toques|no cambies|no borres|i said|i told you|i asked|again\b|still (failing|broken|not|wrong)|"
    r"doesn'?t work|does not work|not what i|that'?s wrong|undo|revert|why did you|don'?t touch|stop)\b")
#: A request that only tells the agent to go on: it stopped to ask what it could have done.
CONTINUATION = re.compile(
    r"^(sigue|segui|continua|continue|go on|go ahead|dale|ok|okay|si|yes|y|procede|proceed|hazlo|do it|adelante)"
    r"[\s.!,]*$")
#: What a request asks for, by its words, when cauce's routing rules (narrow on
#: purpose: Haiku reads the rest) say nothing. First match wins; matched on folded text.
ACTIVITIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((name, re.compile(rx)) for name, rx in (
    ("review", r"\b(revis\w*|review\w*|audit\w*|code review)\b"),
    ("plan", r"\b(planific\w*|plan|planea\w*|disen\w* (la|el|una|un) (arquitectura|api|esquema|sistema)|"
             r"arquitectura|architecture|propon\w*|propos\w*|roadmap)\b"),
    ("debug", r"\b(falla\w*|fallo|error\w*|bug\w*|no funciona|no anda|crash\w*|arregl\w*|fix\w*|debug\w*|"
              r"depur\w*|broken|roto|rota)\b"),
    ("test", r"\b(tests?|pruebas?|testing|coverage|cobertura)\b"),
    ("docs", r"\b(readme|docs?|documenta\w*|changelog|docstrings?)\b"),
    ("ui", r"\b(pantalla\w*|vista\w*|ui|ux|css|estilos?|layout|boton\w*|button\w*|screens?|componente?s?|"
           r"responsive|paleta|palette|colou?r\w*|redisen\w*|redesign)\b"),
    ("refactor", r"\b(refactor\w*|reorganiz\w*|limpia\w*|clean ?up|simplific\w*|simplif\w*|renombr\w*|rename)\b"),
    ("release", r"\b(commit\w*|push\w*|pr|pull request|merge\w*|deploy\w*|despleg\w*|release|publica\w*)\b"),
    ("implement", r"\b(implement\w*|agreg\w*|anad\w*|crea\w*|haz|hace\w*|add|create|build|cambi\w*|change|"
                  r"actualiz\w*|update|migr\w*|integr\w*|conect\w*)\b"),
    ("explore", r"\b(donde|where|busca\w*|find|explica\w*|explain|que hace|what does|como funciona|how does|"
                r"entiende|understand|analiz\w*|analy[sz]\w*|investig\w*)\b"),
))
#: A request this long is a paste: a log, an error, a document.
PASTE_CHARS = 2000
#: Commands that check work.
CHECK = re.compile(
    r"\b(pytest|unittest|jest|vitest|mocha|playwright|cypress|go (test|vet)|cargo (test|clippy|check)|ruff|eslint|"
    r"prettier|tsc|mypy|pyright|biome|rspec|phpunit|mvn (test|verify)|gradle\w* (test|check)|make (test|check|lint)|"
    r"(npm|pnpm|yarn|bun) (run )?(test|lint|typecheck|check|build)|coverage|dotnet test|swift test|plugin validate)\b")
COMMIT = re.compile(r"\bgit\s+commit\b")
PUSH = re.compile(r"\bgit\s+push\b")
PR = re.compile(r"\bgh\s+pr\s+create\b")
PR_TOOLS = frozenset({"mcp__github__create_pull_request"})
AGENT_TOOLS = frozenset({"Agent", "Task"})
COMMAND_NAME = re.compile(r"<command-name>\s*/?([\w:.-]+)\s*</command-name>")
REFUSED = ("user doesn't want to proceed", "the user doesn't want", "rejected")
INTERRUPTED = "[Request interrupted"
#: A line of an error that names what went wrong, preferred to one that only says something failed.
ERROR_NAMED = re.compile(r"(\w+(Error|Exception)\b|ERR!|panic:|fatal:|no such file|not found|denied|refused|"
                         r"cannot find|undefined)", re.IGNORECASE)
#: A line of an error that says what went wrong, preferred to the first line.
ERROR_LINE = re.compile(r"(error|ERR!|failed|FAIL|Traceback|exception|not found|no such file|cannot|denied|"
                        r"refused|invalid|missing|undefined)", re.IGNORECASE)
#: A conventional commit's type, and a subject that fixes something without one.
CONVENTIONAL = re.compile(r"^(\w+)(\([^)]*\))?!?:")
FIX_WORDS = re.compile(r"\b(fix\w*|bug\w*|arregl\w*|correg\w*|repar\w*|hotfix|patch)\b", re.IGNORECASE)
AGENT_TRAILER = re.compile(r"co-authored-by:\s*claude|generated with \[claude code\]", re.IGNORECASE)


def _clip(text: str, limit: int) -> str:
    """One line of at most `limit`, `<private>` text out and secrets masked."""
    return compact._clip(_PRIVATE.sub(" ", text or ""), limit)


def _when(entry: Mapping[str, Any]) -> str | None:
    stamp = entry.get("timestamp")
    return stamp if isinstance(stamp, str) else None


def _minutes(first: str | None, last: str | None) -> float | None:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds() / 60)


def command_key(command: str) -> str:
    """What a shell command is, without its arguments: `cd x && uv run pytest -q tests/` → `uv run pytest`."""
    segments = [s.strip() for s in re.split(r"&&|\|\||;|\n", command or "") if s.strip()]
    segments = [s for s in segments if not re.match(r"^(cd|export|source|\.)\s", s)] or segments
    if not segments:
        return ""
    out: list[str] = []
    for word in segments[-1].split():
        if not out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            continue  # an environment assignment before the program
        if out and ((word.startswith("-") and word != "-m") or re.search(r"[/=\"'`$<>|.]", word)):
            break
        out.append(word.rsplit("/", 1)[-1] if not out else word)
        if len(out) == 3:
            break
    return " ".join(out)


def activity(text: str) -> str | None:
    """What a request asks for: cauce's kind when a routing rule (or a tag) reads it,
    else the first activity its words name, else None."""
    found = classify.by_rules(text)
    if found is not None:
        return classify.asks_for_change(text, found).kind
    folded = fold(text)
    if CONTINUATION.match(folded.strip()):
        return None
    return next((name for name, rx in ACTIVITIES if rx.search(folded)), None)


def error_line(text: str) -> str:
    """The line of an error that says what went wrong."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^(exit code \d+|[=_*#-]{3,}.*[=_*#-]{3,})$", ln, re.IGNORECASE)
             ] or lines
    for pattern in (ERROR_NAMED, ERROR_LINE):
        for ln in lines:
            if pattern.search(ln):
                return ln
    return lines[0] if lines else ""


def error_signature(text: str) -> str:
    """An error with what changes from run to run taken out, to tell one error seen twice."""
    line = fold(error_line(text))
    line = re.sub(r"(/[\w.@-]+)+", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", line)
    line = re.sub(r"\d+", "#", line)
    return " ".join(line.split())[:160]


# --- one session ------------------------------------------------------------------------


def _entries(path: Path) -> Iterable[dict[str, Any]]:
    read = 0
    with path.open("rb") as fh:
        for line in fh:
            read += len(line)
            if read > MAX_BYTES:
                return
            if len(line) > compact.MAX_LINE:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_session(path: Path) -> dict[str, Any]:
    """What happened in one session, from its transcript."""
    facts: dict[str, Any] = {
        "id": path.stem, "cwd": None, "branch": None, "first": None, "last": None, "requests": [],
        "kinds": [], "commands": Counter(), "checks": 0, "commits": 0, "pushes": 0, "prs": 0, "plan_mode": 0,
        "skills": Counter(), "subagents": Counter(), "slash": Counter(), "models": Counter(),
        "tool_errors": Counter(), "interrupts": 0, "refusals": 0, "corrections": [], "continuations": 0,
        "pastes": 0, "pairs": [], "unsolved": [], "written": 0,
    }
    calls: dict[str, tuple[str, str]] = {}
    failing: dict[str, dict[str, Any]] = {}
    written_since: list[str] = []
    said = ""
    for entry in _entries(path):
        if entry.get("isSidechain"):
            continue
        facts["cwd"] = facts["cwd"] or (entry.get("cwd") if isinstance(entry.get("cwd"), str) else None)
        facts["branch"] = facts["branch"] or entry.get("gitBranch") or None
        stamp = _when(entry)
        if stamp:
            facts["first"] = facts["first"] or stamp
            facts["last"] = stamp
        kind = entry.get("type")
        message = entry.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant":
            if message.get("model") and message.get("model") != "<synthetic>":
                facts["models"][usage.family(message["model"])] += 1
            text = _text_blocks(content)
            if text.strip():
                said = text
        if kind == "user":
            raw = _text_blocks(content)
            for name in COMMAND_NAME.findall(raw):
                facts["slash"][name] += 1
            if INTERRUPTED in raw:
                facts["interrupts"] += 1
            typed = compact._typed(entry)
            if typed and not (facts["requests"] and facts["requests"][-1] == typed):
                _request(facts, typed, said)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use":
                name, args = str(block.get("name", "")), block.get("input") or {}
                if not isinstance(args, dict):
                    args = {}
                what = str(args.get("command") or "") if name == "Bash" else ""
                calls[str(block.get("id"))] = (name, what)
                _tool_use(facts, name, args, what, written_since)
            elif kind == "user" and block.get("type") == "tool_result":
                name, what = calls.get(str(block.get("tool_use_id")), ("?", ""))
                result = compact._result_text(block)
                if block.get("is_error"):
                    if any(r in result[:200] for r in REFUSED):
                        facts["refusals"] += 1
                        continue
                    facts["tool_errors"][name] += 1
                    if name == "Bash" and what:
                        _failed(failing, what, result, facts["last"], written_since)
                elif name == "Bash" and what:
                    _passed(facts, failing, what, said, written_since)
    for key, open_ in failing.items():
        facts["unsolved"].append({"command": key, "error": open_["error"], "signature": open_["signature"],
                                  "tries": open_["tries"]})
    return facts


def _request(facts: dict[str, Any], typed: str, said: str) -> None:
    facts["requests"].append(typed)
    folded = fold(typed).strip()
    kind = activity(typed)
    if kind is not None:
        facts["kinds"].append(kind)
    if CONTINUATION.match(folded):
        facts["continuations"] += 1
    elif CORRECTION.search(folded):
        facts["corrections"].append({"request": _clip(typed, REQUEST_CHARS), "after": _clip(said, SAID_CHARS)})
    if len(typed) >= PASTE_CHARS:
        facts["pastes"] += 1


def _tool_use(facts: dict[str, Any], name: str, args: Mapping[str, Any], what: str, written: list[str]) -> None:
    if name in compact.WRITERS:
        target = args.get("file_path") or args.get("notebook_path")
        if isinstance(target, str):
            facts["written"] += 1
            written.append(target)
    elif name == "Skill" and isinstance(args.get("skill"), str):
        facts["skills"][args["skill"]] += 1
    elif name in AGENT_TOOLS:
        facts["subagents"][str(args.get("subagent_type") or "general-purpose")] += 1
    elif name == "ExitPlanMode":
        facts["plan_mode"] += 1
    elif name in PR_TOOLS:
        facts["prs"] += 1
    if name == "Bash" and what:
        key = command_key(what)
        if key:
            facts["commands"][key] += 1
        facts["checks"] += bool(CHECK.search(what))
        facts["commits"] += bool(COMMIT.search(what))
        facts["pushes"] += bool(PUSH.search(what))
        facts["prs"] += bool(PR.search(what))


def _failed(failing: dict[str, dict[str, Any]], what: str, result: str, stamp: str | None,
            written: list[str]) -> None:
    key = command_key(what)
    if not key:
        return
    open_ = failing.get(key)
    if open_ is None:
        failing[key] = {"error": _clip(error_line(result), ERROR_CHARS), "signature": error_signature(result),
                        "tries": 1, "since": len(written), "when": stamp}
    else:
        open_["tries"] += 1


def _passed(facts: dict[str, Any], failing: dict[str, dict[str, Any]], what: str, said: str,
            written: list[str]) -> None:
    key = command_key(what)
    open_ = failing.pop(key, None)
    if open_ is None:
        return
    changed = list(dict.fromkeys(written[open_["since"]:]))
    facts["pairs"].append({
        "command": key, "error": open_["error"], "signature": open_["signature"], "tries": open_["tries"],
        "passed_with": _clip(what, COMMAND_CHARS), "changed": [Path(p).name for p in changed][-5:],
        "said": _clip(said, SAID_CHARS), "when": open_["when"],
    })


def shape(facts: Mapping[str, Any]) -> str:
    """A session in a few steps: the kinds its requests were read as, then how it ended."""
    steps: list[str] = []
    for kind in facts["kinds"]:
        if not steps or steps[-1] != kind:
            steps.append(kind)
    steps = steps[:SHAPE_STEPS]
    if facts["plan_mode"] and "plan" not in steps:
        steps.insert(0, "plan")
    if facts["checks"]:
        steps.append("check")
    if facts["commits"]:
        steps.append("commit")
    if facts["prs"]:
        steps.append("pr")
    return " → ".join(steps) or "talk"


# --- one repository's history ------------------------------------------------------------


def _git(top: Path, *args: str, timeout: int = 20) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, timeout=timeout,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def history(top: Path, *, limit: int = COMMITS) -> dict[str, Any] | None:
    """A checkout's last `limit` commits: cadence, kinds, fixes, reverts, agent share, hot and fragile files."""
    raw = _git(top, "log", f"-n{limit}", "--no-merges", "--date=short", "--name-only",
               "--pretty=format:%x1e%h%x1f%ad%x1f%s%x1f%b%x1d")
    if raw is None:
        return None
    commits = []
    for record in raw.split("\x1e"):
        if "\x1d" not in record:
            continue
        head, _, names = record.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        commits.append({"sha": parts[0].strip(), "date": parts[1], "subject": parts[2], "body": parts[3],
                        "files": [n.strip() for n in names.splitlines() if n.strip()]})
    if not commits:
        return {"commits": 0}
    types: Counter[str] = Counter()
    hot: Counter[str] = Counter()
    fragile: Counter[str] = Counter()
    fixes, reverts, agent = [], 0, 0
    for c in commits:
        match = CONVENTIONAL.match(c["subject"])
        if match:
            types[match.group(1).lower()] += 1
        is_revert = c["subject"].lower().startswith("revert")
        reverts += is_revert
        agent += bool(AGENT_TRAILER.search(c["body"]))
        hot.update(c["files"])
        if not is_revert and ((match and match.group(1).lower() == "fix") or FIX_WORDS.search(c["subject"])):
            fixes.append(c)
            fragile.update(c["files"])
    dates = sorted(c["date"] for c in commits)
    weeks = max(1.0, (_minutes(f"{dates[0]}T00:00:00", f"{dates[-1]}T00:00:00") or 0) / (60 * 24 * 7))
    parents = _git(top, "log", f"-n{limit}", "--pretty=format:%p") or ""
    merges = sum(1 for line in parents.splitlines() if len(line.split()) > 1)
    refs = _git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes") or ""
    prefixes: Counter[str] = Counter()
    for ref in refs.split():
        name = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        if "/" in name:
            prefixes[name.split("/", 1)[0] + "/"] += 1
    tags = len((_git(top, "tag", "--list") or "").split())
    return {
        "commits": len(commits), "first": dates[0], "last": dates[-1],
        "per_week": round(len(commits) / weeks, 1), "types": dict(types.most_common(8)),
        "fix_share": round(len(fixes) / len(commits), 2), "reverts": reverts, "merges": merges,
        "agent_share": round(agent / len(commits), 2), "tags": tags,
        "branch_prefixes": dict(prefixes.most_common(6)),
        "hot_files": [f for f, _ in hot.most_common(10)],
        "fragile_files": [{"file": f, "fixes": n} for f, n in fragile.most_common(8) if n >= 2],
        "fixes": [{"sha": c["sha"], "date": c["date"], "subject": _clip(c["subject"], COMMAND_CHARS),
                   "files": c["files"][:4]} for c in fixes[:SUBJECTS]],
    }


# --- every session -----------------------------------------------------------------------


def transcripts(claude: Path, *, days: int = DAYS, limit: int = MAX_SESSIONS) -> tuple[list[Path], int]:
    """The session transcripts touched in the last `days`, newest first, and how many older ones were left."""
    folder = claude / "projects"
    if not folder.is_dir():
        return [], 0
    cutoff = time.time() - days * 86400
    found, old = [], 0
    for path in folder.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            old += 1
        else:
            found.append((mtime, path))
    found.sort(reverse=True)
    return [p for _, p in found[:limit]], old + max(0, len(found) - limit)


def _never(cwd: Path) -> bool:
    resolved = cwd.resolve()
    return resolved == resolved.parent or resolved == Path.home().resolve()


def _project(store: Store | None, key: str, top: Path | None, sessions: list[dict[str, Any]],
             history_limit: int) -> dict[str, Any]:
    requests = [r for s in sessions for r in s["requests"]]
    kinds = Counter(k for s in sessions for k in s["kinds"])
    shapes = Counter(shape(s) for s in sessions)
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for s in sessions:
        for p in s["pairs"]:
            seen = pairs.setdefault((p["command"], p["signature"]), {**p, "sessions": 0})
            seen["sessions"] += 1
    unsolved: Counter[tuple[str, str]] = Counter()
    unsolved_text: dict[tuple[str, str], str] = {}
    for s in sessions:
        for u in s["unsolved"]:
            unsolved[(u["command"], u["signature"])] += 1
            unsolved_text[(u["command"], u["signature"])] = u["error"]
    commands: Counter[str] = Counter()
    for s in sessions:
        commands.update(s["commands"])
    minutes = [m for m in (_minutes(s["first"], s["last"]) for s in sessions) if m is not None]

    def total(field: str) -> Counter[str]:
        out: Counter[str] = Counter()
        for s in sessions:
            out.update(s[field])
        return out

    entry: dict[str, Any] = {
        "repo": key, "dir": str(top or sessions[0]["cwd"]), "exists": top is not None,
        "sessions": len(sessions), "first_seen": min((s["first"] for s in sessions if s["first"]), default=None),
        "last_seen": max((s["last"] for s in sessions if s["last"]), default=None),
        "requests": len(requests),
        "per_session": {"requests": statistics.median([len(s["requests"]) for s in sessions]),
                        "minutes": round(statistics.median(minutes)) if minutes else None},
        "kinds": dict(kinds.most_common()),
        "unclassified": len(requests) - sum(kinds.values()),
        "shapes": [{"shape": sh, "sessions": n} for sh, n in shapes.most_common(6)],
        "openers": [_clip(s["requests"][0], REQUEST_CHARS) for s in sessions if s["requests"]][:OPENERS],
        "friction": {
            "corrections": sum(len(s["corrections"]) for s in sessions),
            "interrupts": sum(s["interrupts"] for s in sessions),
            "refusals": sum(s["refusals"] for s in sessions),
            "continuations": sum(s["continuations"] for s in sessions),
            "pastes": sum(s["pastes"] for s in sessions),
            "samples": [c for s in sessions for c in s["corrections"]][:SAMPLES],
        },
        "error_fixes": sorted(pairs.values(), key=lambda p: (-p["sessions"], -p["tries"]))[:PAIRS],
        "unsolved": [{"command": c, "error": unsolved_text[(c, sig)], "sessions": n}
                     for (c, sig), n in unsolved.most_common(10)],
        "tool_errors": dict(total("tool_errors").most_common(8)),
        "commands": [{"command": c, "runs": n, "check": bool(CHECK.search(c))} for c, n in commands.most_common(12)],
        "skills": dict(total("skills").most_common(10)),
        "subagents": dict(total("subagents").most_common(10)),
        "slash_commands": dict(total("slash").most_common(10)),
        "models": dict(total("models").most_common()),
        "plan_mode": sum(s["plan_mode"] for s in sessions),
        "commits": sum(s["commits"] for s in sessions), "prs": sum(s["prs"] for s in sessions),
        "history": history(top, limit=history_limit) if top is not None else None,
        "cauce": _cauce(store, key),
    }
    return entry


def _cauce(store: Store | None, key: str) -> dict[str, Any] | None:
    """What cauce kept of a project's tasks, when it worked there."""
    if store is None:
        return None
    from cauce import insights

    found = insights.work(store, key)
    if not found["tasks"]:
        return {"tasks": 0, "team": teams.of(key)[0].name}
    return {"tasks": found["tasks"], "team": teams.of(key)[0].name, "failures": found["failures"],
            "by_kind": [{k: r[k] for k in ("kind", "tasks", "pass_rate")} for r in found["by_kind"][:6]]}


def gather(store: Store | None = None, *, days: int = DAYS, limit: int = MAX_SESSIONS, per_project: int = PER_PROJECT,
           only: str | None = None, history_limit: int = COMMITS,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every session of the last `days` grouped by project, each with its history,
    the shapes of work across projects, and what the person already has."""
    claude = roles.claude_dir(env)
    paths, old = transcripts(claude, days=days, limit=limit)
    workers = home().resolve()
    skipped = Counter({"older": old})
    keys: dict[str, tuple[str, Path | None] | None] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    tops: dict[str, Path | None] = {}
    for path in paths:
        try:
            facts = read_session(path)
        except OSError:
            skipped["unreadable"] += 1
            continue
        if not facts["requests"]:
            skipped["empty"] += 1
            continue
        cwd = facts["cwd"]
        if not cwd:
            skipped["no directory"] += 1
            continue
        if cwd not in keys:
            keys[cwd] = _key(Path(cwd), workers)
        found = keys[cwd]
        if found is None:
            skipped["not a project"] += 1
            continue
        key, top = found
        if only is not None and key != only:
            continue
        group = grouped.setdefault(key, [])
        if len(group) >= per_project:
            skipped["over the per-project limit"] += 1
            continue
        group.append(facts)
        tops[key] = tops.get(key) or top
    projects = [_project(store, key, tops[key], sessions, history_limit)
                for key, sessions in sorted(grouped.items(), key=lambda kv: -len(kv[1]))]
    flows: dict[str, dict[str, Any]] = {}
    for p in projects:
        for row in p["shapes"]:
            flow = flows.setdefault(row["shape"], {"shape": row["shape"], "sessions": 0, "projects": []})
            flow["sessions"] += row["sessions"]
            flow["projects"].append(p["repo"])
    return {
        "claude_dir": str(claude), "days": days,
        "read": {"transcripts": len(paths), "sessions": sum(p["sessions"] for p in projects),
                 "projects": len(projects), "skipped": dict(skipped)},
        "projects": projects,
        "flows": sorted(flows.values(), key=lambda f: (-len(f["projects"]), -f["sessions"]))[:15],
        "friction": {k: sum(p["friction"][k] for p in projects)
                     for k in ("corrections", "interrupts", "refusals", "continuations", "pastes")},
        "have": have(env),
    }


def _key(cwd: Path, workers: Path) -> tuple[str, Path | None] | None:
    """A session directory's project key and checkout, or None for no project's
    (the home directory, `/`, a cauce worker's worktree)."""
    try:
        if _never(cwd) or cwd.resolve().is_relative_to(workers):
            return None
    except OSError:
        return None
    if not cwd.is_dir():
        return str(cwd), None
    top = repo.toplevel(cwd)
    if top is not None and _never(top):
        top = None
    return repo.key(top or cwd) or str(cwd), top


def have(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What the person already has: roles, teams, workflows, Claude Code subagents and skills."""
    from cauce import insights, workflows

    catalog = roles.load()
    found_teams, _ = teams.listing(catalog)
    skills_dir = roles.claude_dir(env) / "skills"
    return {
        "roles": [{"name": r.name, "scope": r.scope, "kinds": list(r.kinds), "extends": r.extends}
                  for r in catalog.roles.values()],
        "teams": [{"name": t.name, "scope": t.scope, "roles": list(t.role_names),
                   "projects": teams.projects_of(t.name)} for t in found_teams],
        "workflows": [{"name": w["name"], "scope": w["scope"], "steps": w.get("steps", [])}
                      for w in workflows.listing(None) if not w["shadowed"]],
        "claude_agents": insights.claude_agents(env),
        "claude_skills": sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file())
        if skills_dir.is_dir() else [],
        "cauce_skills": [s.name for s in roles.bundled_skills()],
    }


# --- output ------------------------------------------------------------------------------


def text(found: Mapping[str, Any]) -> str:
    """The same, for a person reading a terminal."""
    read = found["read"]
    lines = [f"{read['sessions']} sessions in {read['projects']} projects over {found['days']} days "
             f"(from {found['claude_dir']})"]
    left = [f"{n} {why}" for why, n in read["skipped"].items() if n]
    if left:
        lines.append("  left out: " + ", ".join(left))
    for p in found["projects"]:
        lines.append(f"{p['repo']} ({p['dir']}): {p['sessions']} sessions, {p['requests']} requests")
        if p["kinds"]:
            lines.append("  asked: " + ", ".join(f"{k} {n}" for k, n in list(p["kinds"].items())[:6]))
        if p["shapes"]:
            lines.append("  shapes: " + "; ".join(f"{s['shape']} ×{s['sessions']}" for s in p["shapes"][:3]))
        f = p["friction"]
        lines.append(f"  friction: {f['corrections']} corrections, {f['interrupts']} interrupts, "
                     f"{f['refusals']} refused calls, {f['continuations']} bare 'go on's")
        if p["error_fixes"]:
            lines.append(f"  errors solved in-session: {len(p['error_fixes'])}; unsolved: {len(p['unsolved'])}")
        checks = [c["command"] for c in p["commands"] if c["check"]]
        if checks:
            lines.append("  checks: " + ", ".join(checks[:4]))
        h = p["history"]
        if h and h.get("commits"):
            lines.append(f"  history: {h['commits']} commits since {h['first']}, {h['per_week']}/week, "
                         f"{int(h['fix_share'] * 100)}% fixes, {h['reverts']} reverts, "
                         f"{int(h['agent_share'] * 100)}% with an agent")
    if not found["projects"]:
        lines.append("no Claude Code session found to read; work in a few sessions first")
    for flow in found["flows"][:5]:
        lines.append(f"flow {flow['shape']}: {flow['sessions']} sessions in {len(flow['projects'])} project(s)")
    lines.append("in a session, /cauce:init builds your team, workflows and memory from this")
    return "\n".join(lines)


def as_json(found: Mapping[str, Any]) -> str:
    return json.dumps(found, indent=2, ensure_ascii=False, default=str)
