#!/usr/bin/env python3
"""A `claude` that follows a script, for the scenario tests: cauce runs it as it runs the
real CLI (argv, the brief on stdin, the JSON envelope on stdout), as a real process under
the real dispatcher.

The script (`FAKE_CLAUDE_SCRIPT`, JSON) is a list of rules, each `{"match": regex, "do":
[action, ...]}`: the first rule whose regex is found in the brief answers, with its next
action (the last one repeats). Every call is logged to `FAKE_CLAUDE_LOG` (one JSON line: its
argv, the rule, the action, and the host variables it saw), so a scenario can say what the
worker was given. An action:

- `{"pass": true, "write": {path: text}, "block": {...}}`: writes the files in its directory
  and answers a passing result block (merged with `block`: parts, findings...).
- `"commit": "subject"` beside `write`: the worker commits what it wrote itself, as a real one may;
  `"litter": {path: text}` then leaves files untracked after that commit (a tool's `__pycache__`).
- `{"turns": true, "write": {...}}`: writes, then ends out of turns.
- `{"die": true}`: kills the process that started it (cauce's `run-queued`) and itself, as a
  machine that ran out of memory would.
- `{"limit": "text"}`: writes, then ends as the CLI does when the account's usage limit turns
  the session away (`is_error`, subtype `success`, the text as its result).

Every answer carries a session id: the one `--resume` names, else the host's when it
inherited one (the real CLI does so), else a new one.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path

HOST = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CAUCE_SESSION_ID")


def next_action(script: list[dict], brief: str, state: Path) -> tuple[int, dict]:
    for n, rule in enumerate(script):
        if re.search(rule["match"], brief):
            with open(state, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                counts = json.loads(f.read() or "{}")
                seen = counts.get(str(n), 0)
                counts[str(n)] = seen + 1
                f.seek(0)
                f.truncate()
                f.write(json.dumps(counts))
            return n, rule["do"][min(seen, len(rule["do"]) - 1)]
    return -1, {"pass": True}


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--help"]:
        # cauce asks which lean flags this CLI knows (`launch.lean_flags`).
        print("  --disable-slash-commands\n  --exclude-dynamic-system-prompt-sections")
        return 0
    brief = sys.stdin.read()
    for flag in ("--append-system-prompt-file",):
        if flag in argv:
            brief += Path(argv[argv.index(flag) + 1]).read_text()
    script = json.loads(Path(os.environ["FAKE_CLAUDE_SCRIPT"]).read_text())
    rule, action = next_action(script, brief, Path(os.environ["FAKE_CLAUDE_SCRIPT"] + ".state"))
    # As the real CLI does: a session it inherits from the host is the one it reports.
    inherited = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID")
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else inherited or str(uuid.uuid4())
    if "--fork-session" in argv:
        session = str(uuid.uuid4())
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "rule": rule, "action": action, "cwd": os.getcwd(),
                              "host": {k: os.environ[k] for k in HOST if k in os.environ},
                              "worker_task": os.environ.get("CAUCE_WORKER_TASK")}) + "\n")
    for path, text in (action.get("write") or {}).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    if action.get("commit"):
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", action["commit"]],
                       check=True)
    for path, text in (action.get("litter") or {}).items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    if action.get("die"):
        os.kill(os.getppid(), signal.SIGKILL)
        os.kill(os.getpid(), signal.SIGKILL)
    usage = {"input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 10}
    if action.get("limit"):
        print(json.dumps({"is_error": True, "subtype": "success", "result": action["limit"], "session_id": session,
                          "num_turns": 1, "total_cost_usd": 0.0, "usage": usage}))
        return 0
    if action.get("turns"):
        print(json.dumps({"is_error": True, "subtype": "error_max_turns", "session_id": session,
                          "num_turns": 5, "total_cost_usd": 0.01, "usage": usage}))
        return 0
    block = {"verdict": "pass", "summary": action.get("summary", "done as scripted"),
             "evidence": "fake check: 1 passed", **(action.get("block") or {})}
    result = f"Done.\n```cauce-result\n{json.dumps(block)}\n```\n"
    print(json.dumps({"is_error": False, "result": result, "session_id": session, "num_turns": 3,
                      "total_cost_usd": 0.01, "usage": usage}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
