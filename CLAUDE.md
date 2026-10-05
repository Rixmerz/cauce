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
| `src/cauce/config.py` | cauce's settings (`livespec` on/off), copied from plugin options by SessionStart |
| `src/cauce/orchestrate.py` | the loop: plan, attempt, verify, move, remember |
| `src/cauce/usage.py` | tokens per model from session transcripts, and spend by model and cell |
| `src/cauce/flow.py` | the queue's dispatcher (`cauce work`), serial lanes per repository, the stale sweep |
| `src/cauce/store.py` | SQLite memory: tasks, messages, attempts, problems and fixes |
| `src/cauce/hooks.py` | Claude Code hooks: prompt ↔ task coupling, dead ends into context |
| `src/cauce/isolate.py` | one git worktree per writing task |
| `commands/orchestration.md` | the one entry point a user types |
| `bin/cauce` | the launcher every hook and command goes through |

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
never from the worker. `--verify` overrides a claimed pass. A classifier that
cannot run yields the default kind, not a guess.

## Rules the router keeps

- Effort and model are different dials. Shallow work climbs effort; a wrong
  approach or a repeated answer changes model. Do not collapse them into one
  ladder walk.
- A retry is for work that never ran; it is never an escalation.
- The model's stated confidence about its own work is not an input.
- History may raise where a task starts, never lower it below its ladder.
- Fable needs a person: `#fable` or `--allow-approval`.
- An adopted neighbour is read, never written: the livespec index is opened
  `mode=ro`, and the index is built only through livespec's own CLI. Absent
  and unreadable are different states and neither stops a run.
- Every livespec tool a hint names is in `adapters/livespec.TOOLS`, checked
  against the release in `MINIMUM_VERSION`. Check a name against livespec
  before adding it, and bump `PINNED` only after checking the new release.
- Tests run with `CAUCE_LIVESPEC=off` and an empty `CLAUDE_CONFIG_DIR`; a
  test that wants livespec builds the fixture index in `tests/livespec_fixture.py`.
- A lane pauses only on a task the dispatcher started; a person's own
  `cauce run` never blocks the queue. A GET in the UI never starts work or
  sweeps — housekeeping runs in hooks, the dispatcher and the server thread.
- The capability registry is read from the user's cauce home only. A
  repository must never be able to add an MCP server by committing a file.
