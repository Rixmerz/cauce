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
| hit its turn ceiling | **more turns** once, then split |
| the task contradicts itself or its check | **replan** — no model fixes a wrong task |

A worker's claim of success is not evidence: a pass needs verbatim output, and
`--verify "<command>"` gets the last word.

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

## Install

Requires Python 3.11+ and Claude Code. The core is standard library only.

```sh
claude plugin marketplace add /path/to/cauce
claude plugin install cauce@cauce-dev
```

The plugin ships `bin/cauce`, which Claude Code puts on the Bash tool's `PATH`.
(claude.ai and Cowork do not install plugins with a `bin/` directory; use
Claude Code.)

## Use

In a session: `/orchestration <what you want done>`. The session stays thin
and dispatches; the work happens in workers.

From a shell:

```sh
cauce route "the login redirect drops the query string"        # where it would start, and why
cauce run "the login redirect drops the query string" --verify "pytest -q tests/test_auth.py"
cauce show 12                                                  # attempts, moves and messages
cauce memory search "redirect query string"                    # dead ends, every repo
cauce memory record --problem "redirect drops query" --fix "use url_for(_external)" --outcome failed --why "..."
```

`cauce run` writes in a git worktree on `cauce/task-<id>` and leaves that
branch only when the task passed. `--launch-dir` starts the worker in another
directory (its settings and instructions) while it works in the repository.

### Queue and lanes

Work arrives faster than it runs. Type `++ <task>` in a session and it is
queued without spending a turn; `cauce queue add "<task>" --verify "<cmd>"`
does the same from a shell. `cauce work` drains the queue in workers, never in
the session's context.

Each repository is one **serial lane**: its tasks run one after another, and a
task that ends in anything but a pass **pauses the lane**, so the next one never
starts on a state the last left broken. `cauce lanes` shows them;
`cauce lanes --unpause .` reopens one. `--max` bounds how many tasks one
unattended `cauce work` runs.

A run whose process died leaves no task stuck in `running`: a sweep (at session
start and before every `cauce work`) marks it interrupted and pauses its lane.
`cauce cancel <id>` stops a run; `cauce events --follow` shows runs as they go.

A turn that delegated to a subagent is not done while the subagent still runs,
including one started in the background. A cell a person pinned (`--start`)
never counts as evidence for where the router should start.

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
