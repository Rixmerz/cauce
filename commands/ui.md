---
description: Open cauce's board — what needs you, what is running and what it costs, the queue per repository — in a local web page
---

Start the board in the background and tell the user where it is:

```bash
cauce ui --open
```

Run it with `run_in_background`, so the session stays usable; it serves until
the session ends or the user stops it, and it follows plugin updates by itself.
It listens on `127.0.0.1:8790` only. If it says this same version already
serves that port, the board is there: give the user that address. If it says
an older cauce UI holds the port, tell the user: that server shows the old
version's board until they stop it. Meanwhile, run
`cauce ui --port <another> --open`. If another program holds the port, use
another port too.
When `cauce` alone is not found (exit 127), it is
`${CLAUDE_PLUGIN_ROOT}/bin/cauce`; call it by that path.

Then say, in one line, the address it printed. Do not read the board yourself:
the page is for the user, and `cauce board --json` gives the counts if one is
needed here.
