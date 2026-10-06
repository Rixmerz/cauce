# cauce

One core for Claude Code. You hand it a request; it decides what kind of work
it is, which model and effort it needs, which tools and capabilities belong to
it, runs it in a one-shot worker, checks the result, escalates on evidence, and
remembers what failed — across every repository on the machine.

> *cauce* (Spanish): a riverbed. It does not do the work; it channels it.

## Why

Two problems, one cause.

**Plugins go unused.** In an ordinary session, an MCP server's tools sit in a
list of dozens and get called when the model happens to think of them. A code
index that is always offered and seldom used is the same as one not installed.
cauce attaches capabilities to the worker whose task needs them, and only
those: the model does not have to remember them, and nothing else competes.

**One session, one model, one effort.** A two-line docs fix and an intermittent
race condition run on the same model at the same effort, in the same growing
context. cauce routes each task to its own cell of the model × effort matrix,
starts it cheap where that holds, and climbs only on evidence.

## How it works

```
request ─► classify ─► choose a start ─► worker ─► check ─► pass ──► branch + memory
            rules,      ladder, history    claude -p   result    │
            then Haiku  and complexity     one-shot    block +   └─ fail ─► move ─► worker …
                                                       --verify          (escalate)
```

### Model × effort

The model decides how well the work *notices* things; the effort decides how
*thorough* the same model is. Every effort level has a purpose:

| Effort | For |
|---|---|
| `low` | answers, explanations, commit messages, trivial edits — drops self-checks |
| `medium` | implementing from a clear plan, tests, refactors, docs |
| `high` | debugging with a repro, UI, integration, routine review |
| `xhigh` | features that cross several parts, bugs without a clear cause, hard plans |
| `max` | correctness over cost — only where the level below showed headroom |

Haiku has no effort dial. Fable is reached only by an explicit `#fable` tag or
with `--allow-approval`. `cauce matrix` prints every kind and its ladder:

```
implement        sonnet/medium → sonnet/high → sonnet/xhigh → opus/high
debug-repro      sonnet/high → sonnet/xhigh → opus/high → opus/xhigh
debug-unclear    opus/high → opus/xhigh → opus/max
plan             opus/xhigh → opus/max → fable/high
docs             haiku → sonnet/low → sonnet/medium
```

### Escalation reads the failure, not the ladder

| The evidence says | Move |
|---|---|
| shallow: a bug left, a case missed, no verification | **more effort**, same model |
| wrong approach, or two cells gave the same answer | **next model**, worktree reset |
| timeout, missing tool | **retry** once at the same cell |
| a command the permission settings refused | **blocked** at once — a stronger model gets the same refusal |
| hit its turn ceiling | `--verify` run by cauce; **more turns**, again while the work keeps moving (cap 200), then split |
| the task contradicts itself or its check | **replan** — no model fixes a wrong task |

A worker's claim of success is not evidence: a pass needs verbatim output, and
`--verify "<command>"` gets the last word. The report prints what changed as
git sees it (`changed (from git): …`) beside the worker's own account, so "I
created the routes" is never the only word on it. A worker refused the command
that would check its work, that wrote something and did not call it a failure,
passes only if `--verify` passes in its place.

### Where a task starts

The first cell of its ladder, one rung up when the classifier calls it high
complexity — and higher still when history says so: when half of the last
tasks of the same kind in this repository (or across all of them) passed at a
higher cell, the next one starts there instead of failing its way up again.

### livespec is built in

cauce adopts [livespec](https://github.com/Rixmerz/livespec) — it does not list
it as one more plugin. With the `livespec` setting on (the default), the code
index takes part in every step, at no token cost:

| Step | What livespec contributes |
|---|---|
| before routing | a task whose code implements a critical spec, or whose symbol is called from everywhere, starts one rung up; a review of such code is reviewed as critical |
| before the worker starts | a code map at the top of its prompt: the symbols the request is about, where they are, how many callers, the specs they implement, the tests that exercise them |
| during the work | the worker gets livespec's tools, with a hint for its kind of work and the `workspace` every call needs |
| after a pass | the specs the change touched, and the files outside the change that call what it changed |
| between sessions | an index missing or older than the last commit is (re)built in the background at session start |

cauce reads the index (`.mcp-docs/docs.db`) directly with `sqlite3`, read-only,
and drives livespec through its own CLI. livespec is not installed separately:
cauce runs an installed livespec plugin if there is one, a `livespec` on `PATH`
otherwise, and the pinned release through `uvx` as the last resort.

Switch it off from the plugin settings (`livespec`), with `cauce config
livespec off`, `CAUCE_LIVESPEC=off`, or `--no-livespec` for one task.
`cauce neighbours` shows what cauce sees.

### Memory

One SQLite file in `$CAUCE_HOME` (default `~/.local/share/cauce`):

- every prompt in a session is a **task**; follow-ups typed while it runs and
  its final answer are **messages** of that task, not tasks of their own;
- every worker run is an **attempt** — cell, turns, cost, failure and the move
  that followed. This is the reroute trace the router learns from;
- every fix tried against a problem, and whether it worked, is searchable
  **across repositories**. A matching dead end is put in front of the model
  before it starts (`UserPromptSubmit`) and in every worker's brief.

### Spend

`cauce spend` shows what work really cost over the last days: a session's own
turns by the model that served them (read from its transcript at every `Stop`),
and workers by cell. `cauce route` says what finished tasks of the same kind
cost on average before a run is paid for. Dollars are API rates on the token
counts; on a subscription they measure how much of the plan work used.

### Fixes that stop working

A fix that worked is recorded with the time it was trusted from and, when the
run kept a branch, the commit it is about. When the same problem turns up
again it is **recurring** and that fix is marked disproved — the most useful
dead end there is, because it looked solved. `cauce memory invalidate <fix>
--why "..."` does the same by hand.

### Habits

Every tool call — in sessions and in workers — is recorded as a **signature**:
`bash:pytest`, `edit:.py`, `git-commit`. Never the arguments, the file
contents, the output or the prompt; a final filter keeps a signature to
`[a-z0-9:._-]`. The hook only appends a line to a file, so it stays out of the
way of every call.

- `cauce habits` lists sequences that repeat **and could run as a hook**: an
  edit or a write followed by a command that does something — a formatter, a
  linter, tests, a build — past boolean gates (enough occurrences, in enough
  sessions, nearly always succeeding, nothing destructive); the score only
  ranks what passed. The model looking around (`find → ls`, `cat → cd`)
  repeats in every session too, but no hook could take it over or save a turn,
  so it is never listed. Mining reads every session and repository; installing
  writes to one repository.
- Per kind of task, the steps that came before passing worker attempts go into
  the next worker's brief, so it does not spend turns finding them again.
- `cauce habits install <id> --command "ruff format"` turns a habit that starts
  with an edit into a `PostToolUse` hook in the repository's
  `.claude/settings.local.json`. **Only that command installs one**: nothing in
  cauce does it by itself. A habit that fails three times in a row turns itself
  off; `cauce habits uninstall <id>` removes it; `cauce habits status` shows
  runs and failures.

## Install

Requires Python 3.11+ and Claude Code. The core is standard library only.
When `python3` is older (macOS ships 3.9), cauce runs on a `python3.11`–`3.14`
found beside it; `CAUCE_PYTHON` picks one by hand.

```sh
claude plugin marketplace add Rixmerz/claude-plugins
claude plugin install cauce@rixmerz
```

To work on a clone, `claude plugin marketplace add /path/to/cauce` and
`claude plugin install cauce@cauce-dev` install it from the checkout instead.

The plugin ships `bin/cauce`, which Claude Code puts on the Bash tool's `PATH`.
Where it does not — an older Claude Code, or a session opened before the plugin
was installed — the `SessionStart` hook adds it for the session, and the
commands fall back to the full path. Restart a session that was open during the
install.

For a terminal, outside Claude Code, run `cauce link` once from a session (or
by the plugin's full path). It writes `~/.local/bin/cauce` (`--dir` for
another place), a shim that runs the newest cauce Claude Code has installed, so
a plugin update does not break it; `cauce link --remove` takes it away. It never
replaces a `cauce` it did not write.
(claude.ai and Cowork do not install plugins with a `bin/` directory; use
Claude Code.)

## Use

In a session: `/orchestration <what you want done>`. The session stays thin
and dispatches; the work happens in workers.

From a shell:

```sh
cauce route "the login redirect drops the query string"        # where it would start, and why
cauce run "the login redirect drops the query string" --verify "pytest -q tests/test_auth.py"
cauce run "build the training module" --verify "npm run build" --allow "Bash(npm run build)"
cauce resume 12 --allow "Bash(node:*)"                         # a task that stopped, from its kept work
cauce show 12                                                  # attempts, moves and messages
cauce memory search "redirect query string"                    # dead ends, every repo
cauce memory record --problem "redirect drops query" --fix "use url_for(_external)" --outcome failed --why "..."
```

`cauce run` writes in a git worktree on `cauce/task-<id>`; nothing reaches
your checkout until you merge that branch. A task that passed leaves it to
review; one that stopped for a person (blocked, waiting on approval) leaves it
too, committed as unverified, and `cauce resume <id>` continues on it; one that
failed or must be replanned leaves nothing. Whatever stopped it — a refusal, the
budget, the environment, the approval gate, a cancel from the UI or
`cauce cancel`, a process that died — the report, `cauce show <id>`, the board
and the session that asked for it say which, and give the command that goes on. The worktree gets the checkout's
ignored `node_modules` / `.venv` and your `.claude/settings.local.json` as
links, so a build can run and a worker is allowed what you allowed; anything
else a worker needs to run, grant with `--allow "Bash(npm run build:*)"`
(repeatable; an absolute path takes two slashes: `Read(//abs/path)`). A blocked
report prints the rules it needs, one per program, and `--dry-run` lists the
ones workers in the repository lacked before. A task started with `--no-isolate`
resumes in the checkout too. A one-shot worker has nobody to ask: what its settings do not
allow is refused, and the report names it. `--launch-dir` starts the worker in another
directory (its settings and instructions) while it works in the repository.

Workers run the model aliases (`sonnet`, `opus`, `haiku`), and the installed
Claude Code decides which model each one is. Every attempt records the model
that actually served it. When your own sessions run a newer version than the
workers got, the plan says so. Pin an alias with
`cauce config model sonnet <model id>`; `cauce config model` lists every alias
and what last served it.

### Queue and lanes

Work arrives faster than it runs. Type `++ <task>` in a session and it is
queued without spending a turn; `cauce queue add "<task>" --verify "<cmd>"`
does the same from a shell. Either way the task stays the session's: it shows
under that session in the UI.

A `++` also starts the repository's dispatcher (`autowork`, on by default), so
queued work runs at once in workers, never in the session's context; `cauce
work` starts it by hand. One dispatcher runs per repository, and each task it
starts is its own `cauce run` process.

Whether a task waits is **Haiku's call** (`parallel`, on by default). The first
task of an idle repository starts. One queued behind running work starts beside
it only when Haiku, reading it against everything running and queued ahead,
finds it independent — it needs nothing those produce and is unlikely to change
the same code. Each task can run in its own worktree, so this decision is about
whether the results will merge, not about sharing a checkout. The decision and
its reason stay on the task, it is made once, and up to three tasks run at once
per repository. When Haiku cannot be asked, the task waits: waiting costs
time, a wrong "parallel" costs a broken merge. `cauce config parallel off`
makes every repository one serial lane.

A task that ends in anything but a pass **pauses its repository's lane**, so
nothing new starts on a state it may have left broken. `cauce lanes` shows
them; `cauce lanes --unpause .` reopens one. `--max` bounds how many tasks one
unattended `cauce work` starts.

A run whose process died leaves no task stuck in `running`: a sweep (at session
start and before every `cauce work`) marks it interrupted and pauses its lane.
`cauce cancel <id>` stops a run; `cauce events --follow` shows runs as they go.

A turn that delegated to a subagent is not done while the subagent still runs,
including one started in the background. A cell a person pinned (`--start`)
never counts as evidence for where the router should start.

### The UI

`cauce ui` (or `/cauce:ui` in a session) serves a local page (`http://127.0.0.1:8790/`, `--open` opens it)
for someone running many tasks across many repositories. It answers, in order:
what needs me, what is running and what it costs, and whether the router is
choosing well. It shows one project at a time, picked at the top: never every
repository's cards at once.

- **Board** — *Needs you* (failed, blocked, replan, waiting on approval, a
  branch to merge), *Running* with the live cell, *Queued* with whether each
  task runs beside the others or waits and why, *Done* by day. Narrow it to one
  session. Cancel a task, reopen a lane, start a lane's dispatcher. Every card
  that stopped says who made the call (you, your permission settings, the
  budget, the environment, the worker's verdict, cauce's rules), the reason,
  what was refused, and the command that continues it, ready to copy.
- **Sessions** — every Claude Code session cauce saw in the project, with the
  command that resumes it. Open one to review it: its own task list as Claude
  Code keeps it (read from `~/.claude/tasks/<session>/`, never written), the
  work it gave cauce, and its recent turns.
- **Task** — why it stopped (the full reason, the refused rules, the last
  worker's own account, the command), the ladder with each attempt on the cell
  it ran at, the attempt timeline with every move as from → to, the dial it
  turned and the evidence for it (the failure, the rule, the cells skipped), what each attempt was refused and the evidence of each
  failure, the plan, the coupled messages, the branch and the merge command.
- **Routing** — per kind, where tasks started and passed, and how each ladder
  was really climbed: every move with the failure behind it and how often the
  next attempt passed. The evidence for tuning a ladder.
- **Spend**, **Memory**, **Habits** — the same numbers as the CLI.

Work is never typed into the page: it is asked for in a session, where it has
the session's context and its record.

**A running UI follows updates.** A server keeps the code it started with, so
one started before a `claude plugin update` would go on showing the old board.
Every 30 seconds it checks what Claude Code has installed; when a newer cauce
is there, it runs that version's UI in its own place, on the same port, and
the open page reloads. `cauce ui` on a port this same version already serves
says where it is; on a port an older one holds, it says so, and stopping that
one is yours. A UI from before 0.4.1 cannot follow updates itself: stop it
once and start `cauce ui` again.

**Enrolled projects only.** A project shows in the UI when it has a `.cauce/`
folder — at the top of its checkout, or where the session runs. cauce creates
it the first time work is queued or run there (`++`, `cauce queue add`, `cauce
run`); `cauce init` does it by hand. The folder ignores itself in git. Having
the plugin on while chatting somewhere does not list that place, and neither
does a `.cauce/` above the project: one in a folder that holds several projects,
or in your home directory, enrolls no session below it. The home directory and
`/` are never a project.

**Session names.** Haiku names each session of an enrolled project after what
it has been asked, in `.cauce/sessions.json`, after its first prompt and again
every ten (`names`, on by default). Change a `name` there by hand and it is
yours: Haiku never renames that session again.

It reads the SQLite file every other part of cauce writes and owns no state of
its own. It binds `127.0.0.1` only, checks the `Host` header, and takes
mutations only as JSON with a token kept in a `0600` file in cauce's home —
delete the file and every open tab loses its write access. A GET never starts
work. `cauce board --json` prints the board's counts as typed JSON for a status
line; `cauce board --full --repo <dir>`, `cauce show <id> --json` and
`cauce queue add … --json` are the surface another program builds on. [docs/ui.md](docs/ui.md) has the design.

### Capabilities

`$CAUCE_HOME/capabilities.json` — user-level only, never read from a
repository, because a server entry is a command that runs on this machine:

```json
{
  "livespec": {
    "server": {"command": "livespec", "args": []},
    "when": "always",
    "hint": "before editing a symbol, ask who_calls and analyze_impact; workspace=\"{workdir}\""
  },
  "layout-inspector": {
    "server": {"command": "layout-inspector", "args": []},
    "when": ["ui"],
    "after_failure": ["implement", "feature"],
    "hint": "measure the rendered page instead of judging it by eye"
  }
}
```

`always` is transversal, a list of kinds is per task, `after_failure` is
reactive: it joins the next attempt once one of those kinds failed.

## Measured so far

On a real `claude` (2.1.289), a Haiku classification costs about $0.009; a
docs task that Haiku finished on its first attempt cost $0.03; a task whose
check contradicted it was stopped as `replan` after one $0.02 attempt instead
of climbing the ladder. The ladders themselves are starting points, not
measurements — see [docs/design.md](docs/design.md).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m ruff check .
.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report
```

## License

[Apache-2.0](LICENSE). cauce runs livespec as a separate program and reads its
index; livespec itself is AGPL-3.0 and is not included.
