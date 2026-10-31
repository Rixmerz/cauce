#!/usr/bin/env python3
"""A `claude` on PATH that records what each worker was sent and what it read, then answers
as the real CLI did. Each `-p` call appends one line to `$TAP_LOG`: its argv, its appended
system prompt, its stdin (the brief and the result contract) and the CLI's own usage.
Anything else (`--help`, which `launch.lean_flags` reads) goes straight through."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REAL = os.environ.get("TAP_REAL", "/opt/node22/bin/claude")


def main(argv: list[str]) -> int:
    if "-p" not in argv:
        os.execv(REAL, [REAL, *argv])  # noqa: S606
    stdin = sys.stdin.read()
    system = ""
    if "--append-system-prompt-file" in argv:
        system = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
    elif "--append-system-prompt" in argv:
        system = argv[argv.index("--append-system-prompt") + 1]
    began = time.monotonic()
    done = subprocess.run([REAL, *argv], input=stdin, capture_output=True, text=True, check=False)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = {}
    row = {"at": time.time(), "wall_s": round(time.monotonic() - began, 1), "cwd": os.getcwd(),
           "argv": [a if len(a) < 400 else f"<{len(a)} chars>" for a in argv], "system": system, "stdin": stdin,
           "exit": done.returncode, "worker_task": os.environ.get("CAUCE_WORKER_TASK"),
           "run": os.environ.get("TAP_RUN"),
           **{k: out.get(k) for k in ("num_turns", "total_cost_usd", "usage", "modelUsage", "session_id",
                                      "is_error", "subtype")},
           "result_chars": len(out.get("result") or "")}
    log = os.environ.get("TAP_LOG")
    if log:
        with open(log, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(row) + "\n")
    return done.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
