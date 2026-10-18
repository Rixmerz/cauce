# cauce — project instructions

cauce is the core that routes Claude Code work: classify, choose a model ×
effort cell, run a one-shot worker with exactly the tools and capabilities its
task needs, check the result, escalate on evidence, remember what failed. It is
installed as a plugin and its guidance runs in other people's repositories —
treat `commands/` and every prompt string in `src/` with the same care as code.

## Layout

| Path | What lives there |
|---|---|
| `src/cauce/matrix.py` | models, the five efforts and their purpose, the ladder per kind; a model's default effort reads cells recorded before it had the dial |
| `src/cauce/classify.py` | rules first, Haiku for the rest; `frontier` only by tag |
| `src/cauce/escalate.py` | failure kind → move (retry, more effort, next model, more turns, replan) |
| `src/cauce/launch.py` | one attempt as `claude -p`: argv, the result contract, parsing, git-read changes |
| `src/cauce/capabilities.py` | which MCP servers a worker gets, from the user-level registry |
| `src/cauce/mcp_guard.py` | a JSON-RPC guard between a worker and an MCP server: the calls a policy refuses never reach the server; stdlib, imports nothing of cauce |
| `src/cauce/plugins.py` | neighbours the person installed as Claude Code plugins (layout-inspector, rastro), read from the plugin cache or PATH and handed over as capabilities with their skill; `versions` is the one reader of the cache (folders only, never a `temp_*` clone `/plugin` left half done) |
| `src/cauce/roles.py` | what a worker is: role files (bundled `roles/`, the person's in their cauce home), `extends` and `match`, skills found by name and put into the worker's system prompt |
| `src/cauce/teams.py` | the person's teams of roles and which one each project uses (kept in their cauce home); a project's team bounds the kinds it is classified into; `team_cli.py` is `cauce roles …` and `cauce team …` |
| `src/cauce/survey.py` | what this machine says about how the person works (`cauce survey`): every Claude Code session it kept (requests, shapes, friction, errors and the call that passed after them) and each repository's commit history; read only, no model |
| `src/cauce/investigate.py` | `cauce investigate`: one read-only worker per project (and per plugin to take on), each in its own lane, wearing the internal `surveyor` role, briefed with the deep survey; their `cauce-report` blocks kept on the tasks, nothing filed |
| `src/cauce/insights.py` | what the person's projects say about the teams they need (`cauce team insights`): kinds asked and how each went, languages, checks, the team each uses; read only, no model |
| `src/cauce/criteria.py` | a request's listed items as criteria `S1..Sn`, verbatim; a pass answers each |
| `src/cauce/findings.py` | findings as records: where, what, `evidence_class`, `cause`; which block a change |
| `src/cauce/ledger.py` | a review's findings as one frozen ledger `F1..Fn`: who decides each row (refuter, fix, re-check), two rounds, then the person |
| `src/cauce/risk.py` | a change's tier (passive, medium, high) from its request, kind and files; only raised; no model |
| `src/cauce/surfaces.py` | where a task may write (`--surface`, a step's `surfaces`): checked after each attempt, and whether two tasks can never touch the same file |
| `src/cauce/phase.py` | what a running worker is doing (exploring, implementing, checking), read from the tail of the tool-call log |
| `src/cauce/roles_internal/` | roles cauce runs itself and no team holds: the independent `verifier`, and the `surveyor` that investigates for `/cauce:init` |
| `skills/init/` | the plugin's one skill (`/cauce:init`): scope and an estimate of its cost agreed with the person, `cauce investigate` digs, and from the reports one team for every flow, one workflow per flow, and the memory of what was solved, through cauce's own commands only |
| `docs/images/` | the README's screenshots of `cauce ui`, taken on demo data with generic project names |
| `src/cauce/roles/`, `src/cauce/skills/` | the bundled team and the skills it carries; every vendored skill keeps its license, and `skills/README.md` credits it and lists what cauce changed |
| `src/cauce/adapters/` | neighbours the core *adopts*: read their data, brief, route and assess with it. `livespec.py` is the first; `engram.py` reads the team's engram memory over its server and writes to it only opt-in |
| `src/cauce/config.py` | cauce's settings (`livespec`, `parallel`, `autowork`, `names`, `notes`, `scribe`, `prune`, `plugins`, `verifier`, `engram`, `engram_write`, all on/off), copied from plugin options by SessionStart |
| `src/cauce/orchestrate.py` | the loop: plan, attempt, verify, move, remember |
| `src/cauce/signature.py` | the tool-call signature and the append-only log the PostToolUse fast path writes; imports nothing heavy |
| `src/cauce/habits.py` | mining sequences per repository (and the ones several repositories share), recipes for the brief, and approval-gated habit hooks |
| `src/cauce/usage.py` | tokens per model from session transcripts and their subagents' (counted to the delegation that started each), spend by model and cell, and what a task's or a session's own turns cost |
| `src/cauce/flow.py` | the queue's dispatcher (`cauce work`): lanes per repository, each task its own `cauce run-queued` process, the stale sweep |
| `src/cauce/dispatch.py` | Haiku's call on whether a queued task runs beside running work or waits, and the one-dispatcher-per-repository lock the `++` hook starts it under; light, the hook imports it |
| `src/cauce/store.py` | SQLite memory: tasks, messages, attempts, problems and fixes, notes |
| `src/cauce/notes.py` | what a project knows, by topic: notes, typed links between them, anchors to code that send them to review; filed by Haiku, recalled by topic, file and one link away |
| `src/cauce/models.py` | what `--model` gets for an alias (the alias, or a person's pin), the model that served an attempt, and workers lagging the person's sessions |
| `src/cauce/grants.py` | what workers may do: the permission mode per model (auto; Haiku bypasses) and the rules a person keeps per repository with `cauce allow` |
| `src/cauce/runtime.py` | the Node a project declares (`.nvmrc`, `.node-version`, `engines.node`), or its installed dependencies need when it declares none, found among nvm's and put first on a worker's PATH |
| `src/cauce/allow.py` | a refused call → the `--allow` rules that let it through, one per program; suggestions only, nothing is granted |
| `src/cauce/stops.py` | why a task stopped, who made the call, and the command that continues it; one account on its `finished` event; light, the hooks and the UI read it |
| `src/cauce/project.py` | a project's `.cauce/` folder: enrollment (only enrolled projects show in the UI) and `sessions.json`, the session names; stdlib, no git call |
| `src/cauce/workspaces.py` | a folder of repositories that is one project (`cauce workspace`): declared in the person's cauce home, read by `repo.key` and `project.find`; a member's files are named with it in front in the notes |
| `src/cauce/naming.py` | Haiku names a session, started detached by the Stop hook; a name a person wrote is never replaced |
| `src/cauce/ui/` | `cauce ui`: `api.py` turns the store into JSON (testable without a socket), `server.py` is the envelope and the routes, `static/` one ES module per screen, no build step; `svg.js` draws the workflow graphs and the project map node by node |
| `src/cauce/compact.py` | the facts of a session (requests verbatim, failed calls, changed files), written before a compaction and put back after it; no model call |
| `src/cauce/scribe.py` | the scribe: at every turn's end, what a session's transcript gained (requests verbatim, corrections, files written, failed calls and the one that passed after), kept in the cauce home; the thread a task the session sent gets in its brief, when the notes are due, and the relay to a fresh session; no model call |
| `src/cauce/analysis.py` | where cauce's time and money go (`cauce stats`, the UI's Analysis): runs from `planned` to `finished`, their timed phases, attempts by cell, the session's thread in briefs, a session's own turns; read only, no model |
| `src/cauce/hooks.py` | Claude Code hooks: prompt ↔ task coupling, dead ends into context |
| `src/cauce/workflows.py` | saved chains of tasks: definitions in three scopes (project, user, bundled `workflow_templates/`), runs the engine advances when a step passes, each step on the commit the one before kept; `workflow_cli.py` is `cauce workflow …` |
| `hooks/cauce.tsx` | the mod (function hooks), a thin layer over `bin/cauce`: model tools, waking the session on endings, a status line, `/cauce` opens the web board; `hooks/cauce-cli.ts` its engine-free helpers, `types/index.d.ts` its state contract, `hooks/cauce.test.tsx` its tests |
| `src/cauce/services.py` | the servers a task needs up (`--service 'cmd@port'`): started in the task's directory, waited for, stopped with their process group when the run ends |
| `src/cauce/isolate.py` | one git worktree per writing task, the checkout's ignored dependencies linked in; work that stopped for a person kept unverified |
| `commands/orchestration.md` | the one entry point a user types |
| `commands/habits.md` | review the habit candidates for this repository and install the ones the person picks |
| `commands/workflow.md` | make, change, copy, run and watch workflows |
| `commands/watch.md` | a session that watches every other one: `overview`, the endings as they happen, `SendMessage` to send work |
| `commands/relay.md` | `/cauce:relay`: hand the session's thread to the next session started in the same place (`/clear`), instead of compacting |
| `bin/cauce` | the launcher every hook and command goes through |
| `src/cauce/interpreter.py` | which Python runs cauce; imported before the version check, so it must run on any python3 |
| `src/cauce/link.py` | `cauce link`: a shim on a terminal's PATH that survives plugin updates; `forward_to`, which sends a typed command from an old install to the newest (never a hook, never a checkout) |

## Verify before reporting done

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage report
claude plugin validate .
claude plugin test .
```

`claude plugin validate .` also reads the mod. In the hooks module, `$` stays
in the file: helpers that take it are declared at its top level, and imported
helpers never get it.

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
- A retry is for a launch that broke (a timeout before any change, no JSON,
  an errored session); it is never an escalation. An environment the worker
  named, or a check cauce could not run, blocks at once. A refused
  command is neither: it blocks at once, because it is a setting. An
  environment failure on an attempt that changed files is not work that never
  ran: it blocks at once too, its work kept. A timeout after the work changed
  files is out of turns, not the environment: more turns, and an attempt's
  time grows with its turns (`launch.timeout_for`). A network that dropped is
  neither: read from a session's own failed call (`launch.NETWORK_DOWN` and
  `NETWORK_FLAKY`), from a worker that named the environment (`NETWORK_DOWN`
  only: its evidence is a command's output), or from cauce's check only on a
  line where the check itself could not reach it (`launch.CHECK_OFFLINE`),
  never a word a test logs. The same cell goes again after each of
  `escalate.NETWORK_WAITS`, from the work it kept; such a try counts toward no
  attempt ceiling (nor `cli.UNATTENDED_ATTEMPTS`) and is no answer for a
  repeated pair; one that stays down stops the task as `network`, its work
  kept.
- Out of turns is room, never a bug: a finding of the independent check in
  work whose worker ran out of turns before its own check earns the same cell
  more turns, never more effort or another model, and is no dead end.
- Cost is measured and shown, never a brake: every attempt keeps what it cost
  (its own and its independent check's apart, and how much of its input the
  cache served), and the report, the board, `cauce spend` and `cauce stats`
  show it; no task stops, skips a cell or gets a shorter attempt for money,
  and no worker is given a dollar cap. Turns are the one room dial: an attempt
  is bounded by its turns, never above `escalate.MAX_TURNS_CEILING` however
  many are asked for, and the time they earn (`launch.timeout_for`); a run by
  its attempts. A resume nobody approved is refused after
  `cli.UNATTENDED_ATTEMPTS`.
- A check that could not run at all (exit 127, npm's missing script or
  manifest, a tool's minimum Node) is `environment`, never a `code_bug`: its
  exit says nothing about the work, and it is no dead end. npm's EBADENGINE
  warning is not such a line. Dead ends reach a brief only on a strict match.
- A request that opens with a verb of change is never read as a read-only
  kind unless a tag or `--kind` says so.
- The plan says a task needs servers when it tells the worker to start them,
  or mentions one and its check talks to one (or it has no check), and names
  the words that set it off: a mention is no need.
- Work that stopped for a person (blocked, needs approval) is kept on its
  branch, unverified, and so is work that outgrew its turns (a `replan` for
  turns resumes with more); only `failed`, a `replan` for a wrong task and
  `cancelled` drop it.
- A run in the checkout itself (no worktree, and it writes or has a check)
  holds it: a second one there waits, says for which task, and a cancel
  still stops it. A run in a folder that is no checkout holds the folder and
  every checkout right inside it, in sorted order. A run in a worktree never
  waits.
- A task that waits on a person leaves the board by a resume, by their
  dismissal (`cauce dismiss`, a button they press), or by a later task with
  the same text that passed (`superseded`); each records its `stops.Stop` and
  is marked reported, never handed to a session as an ending. A dismissed
  task resumes as what it was: one found wrong is still rewritten.
- Work a session sent is reported to it once when it ends: by the report a
  `cauce run` prints, else by the Stop hook (which blocks the turn's end once
  with the endings) or the next prompt.
- Every ending short of a pass records a `stops.Stop` on its `finished`
  event: a new way for a task to stop adds its cause there, and a status set
  without one is read back and marked `recovered`, never presented as recorded.
  A cause no run records any more (`budget`) keeps its words for the records
  that hold it, and resumes plainly.
  A cancel says where it came from; "a person did it" is never a guess.
- The model's stated confidence about its own work is not an input.
- A request's criteria are its own words: a pass answers every `S#`, one left
  out is `inconclusive` and one not met is a `code_bug`, unless the step judges
  other work (a workflow's review, refute or recheck, a reading kind:
  `LaunchSpec.judging`): a criterion it reports the work under judgment misses
  is its finding, carried forward, and the step passes when it answered what it
  was asked. A step that builds or fixes keeps the rule. Prose has no criteria.
  Items the request puts out of scope (under a line ending in `:` that says
  so, or marked so themselves) are no criteria, and the brief names them.
- A finding blocks only when the change caused or worsened it (or it cannot be
  told) and it is shown, not suspected; a pre-existing one is reported, never
  a blocker. A finding is `deterministic` only with its proof (the command or
  hunk that shows it); one claimed without it is read as `inferential`.
- Reviewers never vote: their findings are added together, so one is enough
  and there is no tie. A row only one saw and could only read from the code is
  decided by a refuter with its own proof, never by a count; a row it cannot
  decide goes to the person. The engine builds the ledger from the steps'
  `finished` findings, never from a worker's words: a fix gets only its rows,
  a re-check answers only for them against the fix's diff, and a pass that
  leaves a row without a verdict is `inconclusive`. Open rows earn one more
  round, up to `ledger.ROUNDS`; then the run stops as `review`.
- A risk tier is only raised, never lowered; a change that cannot be read is
  high. A high change, or a medium one a cheap cell made, is checked by the
  independent verifier before it passes: another cell, read-only, given the
  request verbatim and the diff, never the worker's summary; it runs at high
  effort on the work's model (never Haiku), and keeps only the neighbours that
  measure (`plugins.MEASURES`). A correction round is earned by a blocker that
  persists or by progress (every blocker of the round before fixed and a new
  one a probe shows), at most `MAX_CORRECTIONS`; a new finding after progress
  that is only read from the code goes with the pass, recorded, never a
  blocker; what is left at the cap stops the task as `review`, and so does a ladder that runs
  out (or the attempt limit) while only the verifier's blockers stand on work that
  passed its own check: kept on its branch, never `failed`. A verifier that
  cannot decide keeps the pass and says so. It is told why it runs (the
  risk's reasons) and, for a fix, to show the failure at the base and gone; it
  has its own turns (`VERIFIER_TURNS`), never the worker's raised ones, and is
  never told to stash the work it checks. A resume after a `review` stop (a
  dismissed one too) gives its first check the blockers the `verified` event
  kept.
  Tests run with `CAUCE_VERIFIER=off`.
- Surfaces narrow and grant nothing: a change outside them fails the attempt
  and is undone in its worktree, never in a person's checkout. Two tasks whose
  surfaces are provably disjoint run side by side without Haiku; a task whose
  surfaces may touch another's, or hold a file a running task already changed,
  waits without it, whatever the others declare; in doubt, the dispatcher
  decides as before, and Haiku is shown each task's coordination lines, its
  surfaces and what running work changed so far, never asked to guess them.
- A move explains itself: `Decision.because` is the evidence chain (what the
  attempt ended with, the rule, where it goes) and `skipped` the rungs passed
  over. A new branch in `decide` gives both; the `moved` event keeps them.
- History may raise where a task starts, never lower it below its ladder.
- Fable needs a person: `#fable` or `--allow-approval`. An approval is the
  person's for the run they approved: a resume nobody approved (`--unattended`)
  never uses one an earlier run stored, nor starts on a cell that needs one (a
  `#fable` task's tag is its approval). A resume a session starts on its own
  carries `--unattended` (`commands/orchestration.md`).
- A worker gets a model alias unless a person pinned it (`cauce config model`).
  cauce never hard-codes a model id; the model that served an attempt is read
  from the CLI's `modelUsage`, never assumed from the alias.
- cauce suggests permission rules and never grants one: `allow.py` only
  writes text a person passes with `--allow` or keeps with `cauce allow` or
  `cauce resume --keep` (stored in their cauce home, never in a repository;
  `--keep` is refused with `--unattended`). A rule for an absolute
  path has two slashes (`Read(//abs)`). Three things are allowed with the
  worker itself: the tools of the MCP servers cauce hands it, reading the
  skill folders of its role, and the permission mode its model runs in
  (auto; Haiku bypasses), which a person changes with `cauce config mode`.
- A refused attempt blocks; it never climbs, whatever else it ended with,
  unless the worker found the task wrong. A refused write to a file in the
  worker's own directory that git shows changed anyway is no refusal: it
  stopped nothing.
- A reading task's answer is its deliverable: findings, broken code included,
  are a pass, and two cells reporting the same findings stop it as
  `converged` instead of climbing. A task whose own text says it changes
  nothing (words about the task, never about what it builds) is read as
  reading while it changes nothing, and holds no checkout; a `feature`,
  `implement`, `refactor` or `docs` task never is.
- A kept branch is deleted only when nothing is lost (`isolate.prunable`):
  its commits are on another branch, HEAD holds every file it changed, it
  holds only artifacts, or another kept branch holds the same tree; never one
  checked out, never one of a task that may resume. Its tip is kept on a
  `branch_pruned` event. It runs in hooks and the server thread, never in a
  GET. A task asked again while one with the same text keeps its work points
  at `cauce resume`.
- A worktree gets what the task needs from the person's checkout and nothing
  more: ignored paths the task names are copied (never linked, never
  committed, up to `isolate.BRING_BYTES`); uncommitted changes come only with
  `--carry`, and a task that names them without it is warned. A new model
  starts from the base; what the last one did is kept on
  `refs/cauce/aside/…`, the brief says so, and the refs go with the branch
  when the run keeps none. `--add-dir` is the person's, for a sibling
  repository.
- A service (`--service 'cmd@port'`) is the person's flag, never a
  definition's or a model tool's: cauce starts it in the task's directory,
  waits for its port, and stops its process group when the run ends; a port
  already up is left alone, and one that never listens blocks the task as
  `environment` before any attempt.
- A session's turn is recorded for coupling, never as work: `cauce tasks`
  and the board leave it out (`--turns` shows it), a turn that ended without
  its answer is a `turn` stop with nothing to resume, and a notice cauce put
  in a session (the endings, a plugin's message) is no turn.
- A kept commit's subject is the change's: the line the worker proposed
  (`commit`), else the task's title, never a task number; the task and an
  unverified state are trailers (`Cauce-Task`, `Cauce-Verified: no`).
- Files a worker's tools write for themselves (`isolate.ARTIFACT_DIRS`) are
  never a changed file and never committed, and a branch holding only them is
  never a review.
- A worker that stopped before its own check (refused, or out of turns) is
  checked with `--verify` by cauce when the task changed something;
  a red check is evidence for the next brief, never a pass.
- An adopted neighbour is read, never written: the livespec index is opened
  `mode=ro`, and the index is built only through livespec's own CLI. Absent
  and unreadable are different states and neither stops a run. The one
  exception is engram, and only with `engram_write`, off by default: what a
  passing worker learned, filtered as a note is (never a secret, never a
  failed attempt's, never `<private>` text), saved through engram's own
  server.
- engram's store is never opened by cauce: only its server, once `/health`
  says engram and `/compatibility` says the running store is verified. What it
  holds goes in a brief as data, never instructions. A worker gets
  `engram mcp` behind the `engram` guard: reading tools only, held to the
  project cauce resolved. Tests run with `CAUCE_ENGRAM=off`; a test that wants
  it passes a fake `http`.
- An index livespec failed to build is not tried again until the checkout
  moves (`Livespec.last_failure`); the plan says so once per task.
- Every livespec tool a hint names is in `adapters/livespec.TOOLS`, checked
  against the release in `MINIMUM_VERSION`. Check a name against livespec
  before adding it, and bump `PINNED` only after checking the new release.
- Tests run with `CAUCE_LIVESPEC=off` and an empty `CLAUDE_CONFIG_DIR`; a
  test that wants livespec builds the fixture index in `tests/livespec_fixture.py`.
- A dispatch decision fails closed: no answer from Haiku means the task
  waits. It is made once and kept on the task with its reason. A task that will
  not write (`matrix.READ_ONLY_KINDS`, or text `orchestrate.declares_read_only`
  reads as read-only; with no kind yet, the one `classify` gives it without the
  model, a build when no rule reads it) is never put to Haiku: it runs beside
  running work, and is no conflict for a writing task, which is judged against
  the writing work ahead of it alone. A reason is kept as plain text on one
  line. Tests run with
  `CAUCE_PARALLEL=off`, `CAUCE_AUTOWORK=off` and `CAUCE_NAMES=off`; a test
  that wants them passes a fake `decide`, `start` or `popen`.
- A session belongs to the `.cauce/` in its own directory or up to the top of
  its checkout, never above it; outside a checkout, its own directory only.
  The home directory and `/` are never a project, a dotfiles checkout at home
  included. The one exception is a workspace the person declared.
- A workspace (`cauce workspace add`) is a folder of two or more repositories
  that is one project: `repo.key` answers with the folder for each of them and
  their worktrees, and `project.find` for each of them, so notes, tasks, lanes,
  sessions, the team and the rules kept with `cauce allow` are the folder's (a
  repository keeps its own project-scope workflows). It lives in the person's
  cauce home, never in a repository, and never inside a checkout. Its folder
  holds only its own repositories, since every one under it belongs to it: the
  ones named, linked in when elsewhere, or with none named the ones right
  inside it. Nothing is made before everything is checked. Making one moves
  what each repository kept where it was done in that checkout (`Store.rekey`;
  a clone elsewhere with the same key keeps its own, and its notes are copied,
  not moved), merges a fact two of them kept (a current copy over one to
  review, with both one's anchors and links), and waits while one of them has
  work queued or running. A member's file is named with the member in front in
  the notes (`api/src/x.py`); a member and its worktrees review only its own
  anchors, and the folder each member's in that member. A task in the folder
  holds every checkout in it, a linked one included. cauce offers a workspace
  (a task that reaches a sibling with `--add-dir`: their own folder only when
  it holds exactly them, else a new one beside them) and never makes one for
  the person.
- `.cauce/sessions.json` belongs to the person as much as to Haiku: a `name`
  that differs from `haiku_name` is theirs, and nothing in cauce overwrites it.
- The UI shows one project at a time and takes no task: work is asked for in
  a session. Claude Code's own task files are read, never written.
- A lane pauses only on a task the dispatcher started, and never on one a
  person cancelled or on an investigator's; it opens again by itself when that task leaves the board
  (dismissed, superseded, or resumed to a pass), and the queue starts again; a person's own `cauce run` never blocks the queue. A GET in the UI never starts work or
  sweeps — housekeeping runs in hooks, the dispatcher and the server thread.
- Color in the UI carries meaning and never alone: a state, a kind's family
  and a model's depth each have one tone (`util.js`: `statusBadge`,
  `kindChip`, `cellChip`), always beside their word. The kind hues keep their
  fixed order; a change to them is checked for colour-blind separation in
  both themes. The mod's text carries the same tones as emoji markers.
- The UI builds every node with `textContent` (`h()` in `static/util.js`,
  `s()` in `static/svg.js` for SVG), never `innerHTML`, and sets no inline
  style attribute (the page's policy refuses it; use the CSSOM): task text and worker summaries are untrusted. The token
  never goes in a URL, and every mutation needs it in a header.
- `plan()` never waits on a neighbour's index: a refresh starts in the
  background and the task uses the index as it is. A run says its task number
  and each attempt on stderr as they happen; stdout stays the report.
- `PostToolUse` runs on every tool call: its fast path in `__main__.py`
  imports only `cauce.signature` and appends one line. Do not add a database
  write, a git call or an orchestrator import to it.
- A signature never holds an argument. The final `[a-z0-9:._-]` filter stays
  last, after the semantic scrubbing.
- A compaction digest is read from the transcript and git, never written by
  a model: it may leave things out to fit, never paraphrase. A person's
  words are kept as typed; harness text (commands, reminders) is not theirs.
- The scribe reads a session's transcript, never its context, and never with a
  model: the same rules as a compaction digest, kept as the session goes, in the
  cauce home, secrets masked. Its failure never costs a turn its endings. A
  task a session sent gets the part of the thread it needs in its brief,
  verbatim and marked as context, never instructions (the task's text is what
  to do); an investigation gets none, and reading it never fails a run.
- A relay is the person's (`cauce relay`, `/cauce:relay`): taken once, by the
  next session that starts (or is cleared) in the same place, never by a
  resume, and dropped after `scribe.RELAY_HOURS`. The work the old session
  sent that is open, waits on the person or ended unheard reports to the new
  session from then on, its active workflow runs too; the old session is left
  as it was.
- Habits are per project: mined from one repository's events and gated on
  them alone; a habit of other repositories is offered apart, never counted as
  this one's. An event's repository is read from its task or session, never
  written by the PostToolUse fast path.
- Nothing in cauce writes to a settings file except `cauce habits install`,
  run by a person.
- Problems and fixes are global; notes belong to one project and are never
  read from another (a workspace is one project). A note is written by a passing worker (`learned`), by
  Haiku from a session's conversation (every few turns, when the scribe finds
  them due, before a compaction, at its end), or by a person — never by a failed
  attempt, whose facts no pass confirmed. Haiku files under topics that
  exist and never invents a link; without Haiku a fact is kept by its words.
  A fact that looks like a secret is never kept.
- A note's anchor is reviewed only against this checkout's history: a change
  on a branch not merged yet is not a change. Filing notes never fails a run,
  and a GET in the UI never reviews them. Tests run with `CAUCE_NOTES=off`; a
  test that wants notes turns them on and passes a fake `ask`.
- The mod adds a way in, never a power. Every tool runs `bin/cauce`. `resume`
  runs with `--unattended`, which refuses what only a person may clear, and
  no model input ever reaches `--allow`, `--allow-approval` or `--keep`: only
  a button the person presses in the web board, or on a neighbour's page
  through `cauce resume <id> --pressed`, does, and its rules are read
  from cauce's account of the stop (`stops.resume_args`), never from the
  request; `--pressed` refuses any rule or approval passed with it. The board lives in the web UI only; the mod draws no copy of it. Tool schemas carry no `enum`: the handler
  reads a value and says what cauce takes. Only the reading tools (`recall`,
  `tasks`, `note`) turn an `ask` into an allow; a deny stays a deny. Without the mod, everything still works through
  the classic hooks.
- A watcher reads, never claims: `overview` and `events` take nothing from a
  session, and an ending is delivered only to the session that sent the work.
  cauce has no channel of its own to another session; sending one work is
  Claude Code's `SendMessage`.
- An ending is claimed, not just read, when it is delivered
  (`Store.claim_unreported`). The Stop hook, the next prompt and the mod's
  wake may run at the same moment, and each ending is still handed over once.
  The mod wakes a session only while no turn runs.
- A workflow advances on the engine, never on a session: `workflows.advance`
  runs at the end of every run (`orchestrate.run`), queues what waited on a
  step that passed, inside one write transaction, and starts the dispatcher;
  advancing never fails the run that ended, and an error is a
  `workflow_error` event. The session that started a run hears of it once —
  at its end or when a step waits on the person — and steps the engine went
  past are marked reported. A step that stops stops the run there; it goes on
  by a resume to a pass or `cauce workflow retry`.
- A workflow definition grants nothing and caps nothing: no rule, no
  approval, no capability, no dollar limit; rules, approvals and a run's check
  (`--verify`, for every step that writes and names none) are the person's
  flags on `cauce workflow run`, so a definition a repository commits
  runs with what the person gives, and a `budget_usd` an older one holds is
  dropped. A bundled template is never written: a change copies it to the user
  scope.
- Work is cut where context changes hands, never by role: the phases of one
  task are one worker's (the one that builds writes its tests; the one that
  reproduces fixes). A review step of a change reads blind, as the verifier
  does: the request and the diff, never what earlier steps said of their work;
  one with no diff to read (a plan's critique) gets what came before. Other
  steps get the findings of the steps they wait on as records, beside their
  summaries. The bundled templates follow this; their review chain fixes only
  what the ledger confirms, and costs nothing more when the review is clean.
  A refuter is told whether one review or two found its rows.
- A worker never starts cauce work (`run`, `resume`, `work`, `queue add`,
  `workflow run|retry`, `investigate start` refuse under `CAUCE_WORKER_TASK`),
  and forks are off inside it (`CLAUDE_CODE_FORK_SUBAGENT=0`): what is left
  goes in its result. The main session does what fits in what it holds, sends
  to cauce what earns a worker, and a judgment that needs the conversation to
  Claude Code's own fork, never to cauce (`commands/orchestration.md`).
- A run runs the copy of its definition it started with, never the file. A
  definition an active run uses (in any project) is held: a change becomes a
  pending revision that applies when the last such run ends or is cancelled,
  unless a person passes `--now`; removing it needs `--force`.
- A step's `memory` is read when the step is queued, never written. A note
  comes only from the run's own project; a run with a note or problem that
  cannot be read there does not start, a topic with no note yet is said when
  it starts (the step goes without it), and a note gone since says so in the
  brief. `cauce workflow run --dry-run` checks all of it and queues nothing.
- The capability registry is read from the user's cauce home only. A
  repository must never be able to add an MCP server by committing a file.
  Installed neighbours (`plugins.KNOWN`) are read from the person's Claude
  Code plugin cache or PATH, never a repository; a registry entry of the same
  name wins, and `cauce config plugins off` leaves them out. Every tool a
  neighbour's hint names is in its `tools`. rastro runs with `--routines=off`
  and behind `mcp_guard`, which enforces what its hint asks: no browser
  profile, no saved routine, writes and uploads to loopback hosts only, every
  call in its own session. A policy that matters is enforced there, never only
  asked of the worker in its prompt.
- A role is not a subagent: it adds identity and instructions, never a dial.
  It names no model, no effort and no tool to add; it may take tools away,
  carry or point at skills, and name capabilities, which come only from the
  person's registry and adopted neighbours. Its text goes in
  `--append-system-prompt`, never `--agent`, which would replace Claude
  Code's system prompt and cauce's tool list.
- Roles and teams live in the person's cauce home and in cauce itself, never
  in a repository, and so does which team a project uses: a repository must
  not choose what its workers are told by committing a file. A role or team
  that fails its checks is refused with its reason and costs only itself; a
  role of a bundled name shadows it and says so.
- A project's team bounds its kinds: a task is classified only into the kinds
  the team covers, the nearest kind of the same nature (reading or writing)
  when it would be another. A tag or `--kind` is narrowed the same way and
  says so. A team left with no role serves as the bundled one, and says so.
- The analysis is read, never written, and never by a model. A run's phases
  are kept on its events as it runs (`plan_s`, `queued_s`, `since_start_s`,
  each attempt's `wall_s`, `check_s`, `verifier_s`, the waits for a network
  `wait_s`, the run's `wall_s`); a phase
  a run never measured is left out of the breakdown, never guessed. The session
  thread comparison is observed, never presented as a trial.
- Insights and the survey are read, never written, and never by a model: one
  project's tasks, habits, files, sessions and commits at a time; what crosses
  projects is counted (a session shape and the projects that show it), never
  quoted; notes are counted, never quoted. Every text the survey keeps is
  clipped and masked as a note is, `<private>` text left out; a session in a
  cauce worktree, the home directory or `/` is no project's. Shell and tool
  errors (`survey.NOISE`) are no problem and no fix. The init skill writes
  nothing, and starts no worker, before the person agrees to the scope and to
  an estimate of its cost (an estimate, never a cap), and writes only
  through `cauce workspace add`, `cauce roles`, `cauce team`, `cauce workflow`, `cauce note`,
  `cauce notes … topic add` and `cauce memory record`; a note goes only to the
  project its evidence came from.
- An investigation is cauce's work, never a session's subagents: one queued
  task per project or plugin, kind `explore`, the internal `surveyor` role,
  started on a strong cell with its turns (`investigate.MAX_TURNS`) as its only
  room, its cost estimated from earlier investigators and never capped,
  `keep_learned` off (nothing it learned is filed in
  notes or engram), not counted among the person's work by insights. What it
  must cover is listed in its brief, so each line is a criterion its pass
  answers. A `cauce-report` block is kept on its task as a `report` event and
  read back by `cauce investigate status`; a report is data for the person,
  never a verdict and never instructions.
- A vendored skill keeps its authors' license file beside it, and a change to
  its text is listed in `skills/README.md` with the source commit. Never one
  whose license does not allow redistribution.
