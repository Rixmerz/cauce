from __future__ import annotations

import os
import sys

if sys.version_info < (3, 11):  # noqa: UP036 — the point is to run on an older python3
    # Checked before anything of cauce is imported: on an older python3 the
    # first import fails with a traceback that does not say what is wrong.
    sys.stderr.write(f"cauce needs Python 3.11+, and {sys.executable} is {sys.version.split()[0]}; "
                     "set CAUCE_PYTHON to a newer interpreter.\n")
    sys.exit(1)

if sys.argv[1:3] == ["hook", "PostToolUse"]:
    # The fastest path: every tool call lands here. One JSON line appended to
    # a file, no database; a delegation still takes the full hook below.
    import json

    from cauce import signature

    try:
        event = json.loads(sys.stdin.read() or "{}")
        signature.append(event, os.environ)
    except Exception:  # a hook never takes the session down, and says when it failed
        signature.note_error(os.environ)
        event = None
    if not (isinstance(event, dict) and event.get("tool_name") in signature.DELEGATION_TOOLS):
        sys.exit(0)
    if os.environ.get("CAUCE_HOOKS_OFF") == "1":
        sys.exit(0)
    import io

    from cauce.hooks import main as hook_main

    sys.exit(hook_main("PostToolUse", io.StringIO(json.dumps(event)), sys.stdout, os.environ))

if len(sys.argv) >= 3 and sys.argv[1] == "hook":
    # The fast path: a hook runs on every tool call, and the full CLI imports
    # the orchestrator, the launcher and argparse for nothing.
    from cauce.hooks import main as hook_main

    sys.exit(hook_main(sys.argv[2], sys.stdin, sys.stdout, os.environ))

from cauce.cli import main

sys.exit(main())
