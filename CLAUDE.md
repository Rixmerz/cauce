# cauce — project instructions

cauce is the core that routes Claude Code work: classify, choose a model ×
effort cell, run a one-shot worker with exactly the tools and capabilities its
task needs, check the result, escalate on evidence, remember what failed. It is
installed as a plugin and its guidance runs in other people's repositories —
treat `commands/` and every prompt string in `src/` with the same care as code.

## Layout

| Path | What lives there |
|---|---|
| `src/cauce/matrix.py` | models, the five efforts and their purpose, the ladder per kind |
| `src/cauce/classify.py` | rules first, Haiku for the rest; `frontier` only by tag |
| `src/cauce/escalate.py` | failure kind → move (retry, more effort, next model, more turns, replan) |
| `src/cauce/launch.py` | one attempt as `claude -p`: argv, the result contract, parsing, git-read changes |
| `src/cauce/capabilities.py` | which MCP servers a worker gets, from the user-level registry |
| `src/cauce/adapters/` | neighbours the core *adopts*: read their data, brief, route and assess with it. `livespec.py` is the first |
| `src/cauce/config.py` | cauce's settings (`livespec`, `parallel`, `autowork`, `names`, all on/off), copied from plugin options by SessionStart |
| `src/cauce/orchestrate.py` | the loop: plan, attempt, verify, move, remember |
| `src/cauce/signature.py` | the tool-call signature and the append-only log the PostToolUse fast path writes; imports nothing heavy |
| `src/cauce/habits.py` | mining sequences, recipes for the brief, and approval-gated habit hooks |
| `src/cauce/usage.py` | tokens per model from session transcripts, and spend by model and cell |
| `src/cauce/flow.py` | the queue's dispatcher (`cauce work`): lanes per repository, each task its own `cauce run-queued` process, the stale sweep |
| `src/cauce/dispatch.py` | Haiku's call on whether a queued task runs beside running work or waits, and the one-dispatcher-per-repository lock the `++` hook starts it under; light, the hook imports it |
| `src/cauce/store.py` | SQLite memory: tasks, messages, attempts, problems and fixes |
| `src/cauce/models.py` | what `--model` gets for an alias (the alias, or a person's pin), the model that served an attempt, and workers lagging the person's sessions |
| `src/cauce/allow.py` | a refused call → the `--allow` rules that let it through, one per program; suggestions only, nothing is granted |
| `src/cauce/stops.py` | why a task stopped, who made the call, and the command that continues it; one account on its `finished` event; light, the hooks and the UI read it |
| `src/cauce/project.py` | a project's `.cauce/` folder: enrollment (only enrolled projects show in the UI) and `sessions.json`, the session names; stdlib, no git call |
| `src/cauce/naming.py` | Haiku names a session, started detached by the Stop hook; a name a person wrote is never replaced |
| `src/cauce/ui/` | `cauce ui`: `api.py` turns the store into JSON (testable without a socket), `server.py` is the envelope and the routes, `static/` one ES module per screen, no build step |
| `src/cauce/hooks.py` | Claude Code hooks: prompt ↔ task coupling, dead ends into context |
| `src/cauce/isolate.py` | one git worktree per writing task, the checkout's ignored dependencies linked in; work that stopped for a person kept unverified |
| `commands/orchestration.md` | the one entry point a user types |
| `bin/cauce` | the launcher every hook and command goes through |
| `src/cauce/interpreter.py` | which Python runs cauce; imported before the version check, so it must run on any python3 |
| `src/cauce/link.py` | `cauce link`: a shim on a terminal's PATH that survives plugin updates |

## Verify before reporting done

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage report
claude plugin validate .
```

The coverage floor lives in one place, `[tool.coverage.report] fail_under` in
`pyproject.toml`. Raise it when the real number rises; never lower it to pass.

## Conventions

- **Everything committed is in English** — code, prompts, docs, commit
  messages, PRs. The Spanish in `classify.py` and `text.py` is different and
  stays: it matches what users type.
- **Standard library only** in `src/`. A hook runs on every prompt, and a
  dependency that fails to import there is a session that fails to start.
  Adding one needs a stated reason the stdlib failed.
- **No client or engagement names, ever** — in code, tests, examples or
  commit messages. Describe the shape of a system, never who it belongs to.
- **Tests use the `CAUCE_HOME` fixture** in `tests/conftest.py`. Never bypass
  it: a test that writes to the real database puts fake dead ends in front of
  real sessions.

## Fail open, fail closed

**Hooks fail open.** A hook that raises takes the user's session down, so
`hooks.main` swallows everything — and writes it to `hook-errors.log`, which
the next `SessionStart` reports. Failing open is not failing silently.

**Verdicts fail closed.** A worker with no result block, two blocks, or a pass
without evidence is `inconclusive`, never a pass. Changed files come from git,
never from the worker, and every report prints them beside the worker's
summary — the work directory's checkout and every checkout nested in it. `--verify` overrides a claimed pass; it is the only thing that can
pass an attempt whose worker was refused its own check. Refusals come from the
CLI's `permission_denials`, never from the worker. A classifier that cannot run
yields the default kind, not a guess.

## Rules the router keeps

- Effort and model are different dials. Shallow work climbs effort; a wrong
  approach or a repeated answer changes model. Do not collapse them into one
  ladder walk.
- A retry is for work that never ran; it is never an escalation. A refused
  command is neither: it blocks at once, because it is a setting.
- A request that opens with a verb of change is never read as a read-only
  kind unless a tag or `--kind` says so.
- Work that stopped for a person (blocked, needs approval) is kept on its
  branch, unverified, and so is work that outgrew its turns (a `replan` for
  turns resumes with more); only `failed`, a `replan` for a wrong task and
  `cancelled` drop it.
- Work a session sent is reported to it once when it ends: by the report a
  `cauce run` prints, else by the Stop hook (which blocks the turn's end once
  with the endings) or the next prompt.
- Every ending short of a pass records a `stops.Stop` on its `finished`
  event: a new way for a task to stop adds its cause there, and a status set
  without one is read back and marked `recovered`, never presented as recorded.
  A cancel says where it came from; "a person did it" is never a guess.
- The model's stated confidence about its own work is not an input.
- A move explains itself: `Decision.because` is the evidence chain (what the
  attempt ended with, the rule, where it goes) and `skipped` the rungs passed
  over. A new branch in `decide` gives both; the `moved` event keeps them.
- History may raise where a task starts, never lower it below its ladder.
- Fable needs a person: `#fable` or `--allow-approval`.
- A worker gets a model alias unless a person pinned it (`cauce config model`).
  cauce never hard-codes a model id; the model that served an attempt is read
  from the CLI's `modelUsage`, never assumed from the alias.
- cauce suggests permission rules and never grants one: `allow.py` only
  writes text a person passes with `--allow`. A rule for an absolute path has
  two slashes (`Read(//abs)`).
- A worker that stopped before its own check (refused, out of turns or
  money) is checked with `--verify` by cauce when the task changed something;
  a red check is evidence for the next brief, never a pass.
- An adopted neighbour is read, never written: the livespec index is opened
  `mode=ro`, and the index is built only through livespec's own CLI. Absent
  and unreadable are different states and neither stops a run.
- Every livespec tool a hint names is in `adapters/livespec.TOOLS`, checked
  against the release in `MINIMUM_VERSION`. Check a name against livespec
  before adding it, and bump `PINNED` only after checking the new release.
- Tests run with `CAUCE_LIVESPEC=off` and an empty `CLAUDE_CONFIG_DIR`; a
  test that wants livespec builds the fixture index in `tests/livespec_fixture.py`.
- A dispatch decision fails closed: no answer from Haiku means the task
  waits. It is made once and kept on the task with its reason. Tests run with
  `CAUCE_PARALLEL=off`, `CAUCE_AUTOWORK=off` and `CAUCE_NAMES=off`; a test
  that wants them passes a fake `decide`, `start` or `popen`.
- A session belongs to the `.cauce/` in its own directory or up to the top of
  its checkout, never above it; outside a checkout, its own directory only.
  The home directory and `/` are never a project, a dotfiles checkout at home
  included.
- `.cauce/sessions.json` belongs to the person as much as to Haiku: a `name`
  that differs from `haiku_name` is theirs, and nothing in cauce overwrites it.
- The UI shows one project at a time and takes no task: work is asked for in
  a session. Claude Code's own task files are read, never written.
- A lane pauses only on a task the dispatcher started; a person's own
  `cauce run` never blocks the queue. A GET in the UI never starts work or
  sweeps — housekeeping runs in hooks, the dispatcher and the server thread.
- The UI builds every node with `textContent` (`h()` in `static/util.js`),
  never `innerHTML`: task text and worker summaries are untrusted. The token
  never goes in a URL, and every mutation needs it in a header.
- `PostToolUse` runs on every tool call: its fast path in `__main__.py`
  imports only `cauce.signature` and appends one line. Do not add a database
  write, a git call or an orchestrator import to it.
- A signature never holds an argument. The final `[a-z0-9:._-]` filter stays
  last, after the semantic scrubbing.
- Nothing in cauce writes to a settings file except `cauce habits install`,
  run by a person.
- The capability registry is read from the user's cauce home only. A
  repository must never be able to add an MCP server by committing a file.
