# Changelog

## [Unreleased]

### Changed

- **The dispatcher's parallel call reads what the tasks declare.** Haiku saw
  the first 600 characters of each task, so four builds queued with their own
  migration numbers ("Migration version: 3", "other tasks running in parallel
  take the other numbers", said at the end of a long goal) were read as
  depending on each other and ran one after another. The question now gives
  each task's opening and the lines further on that name a migration, a
  version, a file, another task or work in parallel (migrations and parallel
  work first, then versions and other tasks, then files; within a bound), the surfaces each task declared, and, for a running task,
  the files git shows it changed so far in its worktree. Declared surfaces
  that may touch another task's, or hold a file a running task already
  changed, wait without asking the model, whatever the others declare. No
  answer still means the task waits, and when unsure, Haiku answers false.

### Fixed

- **Work that passed its own check is no longer discarded when the independent
  check's corrections use up the ladder.** A feature started at opus/high
  passed the repository's `--verify` twice; the verifier found a different
  blocker each round, each correction took a rung, and at the top of the ladder
  the task ended `failed`: branch and worktree deleted, 25 files and the money
  gone, before `MAX_CORRECTIONS` could stop it as `review`. When the last
  attempt passed its own check and only the verifier's blockers stand, running
  out of ladder (no stronger cell, two cells agreeing with no model above, or
  the attempt limit) now stops the task as `review`: its work kept on its
  branch, unverified (`Cauce-Verified: no`), the stop saying the ladder ran out
  with the verifier's blockers open and its account naming them, resumed like
  any `review` stop (its first check is given those blockers). A correction
  still climbs the ladder as it did: a blocker the check shows in work that
  passed is a missed defect, which is the shallow kind (more effort, or a
  stronger model when a cheap cell cannot find it); it is not evidence that the
  task is wrong, so running out of rungs is not a failure (`Move.REVIEW`,
  `Decision.because` says so). A `failed` whose attempts never passed a check
  still drops its work.
- **A task resumed after its worker died is reported by its last ending.**
  The notice a session got for a task that died twice and then passed after
  `cauce resume` could say "it changed nothing" with no branch: while the run
  filed its notes, the latest `finished` event on record was an earlier death's.
  The endings, the board, the workflow status and the stop account now read
  the task's final ending (`Store.final_event`: the last `finished` whose
  status is the task's own), and a task with none yet is not reported until
  it has one.
- **One count of a task's changed files.** The `finished` event kept only the
  first 50 paths, so `cauce workflow status` said "50 file(s)" where the
  report and `git diff --stat` said 106. The event now keeps up to 500 paths
  and the whole count (`changed_n`), and the endings notice, the report and
  the workflow status all read it.
- **A kept commit's subject no longer ends in "…".** A long worker line or task
  title was cut with an ellipsis. It is now cut at the last whole word that
  fits (not on a dangling "and", "to", "of"…), and the full text goes first
  in the commit body.
- **A resume after its worker died continues where it was.** A run whose
  process died mid-attempt (a machine restart, a killed session) left its
  worktree registered with the attempt's uncommitted work, and `cauce resume`
  failed on it with `already exists`. The resume now continues in that
  worktree, keeping what the dead attempt wrote.
- **A resume that cannot start no longer strands its task.** `cauce resume`
  whose worktree could not be made (`IsolationError` from `isolate.prepare`)
  raised before the first attempt and left the task `running` under the pid of
  the process that had just exited, so every later resume refused it and only
  the stale sweep freed it. An error before the first attempt now stops the
  task as `environment` with the error's one-line cause, resumable, and a
  resume of a `running` task whose process is gone sweeps it first instead of
  refusing. A failure of cauce itself under a resume reaches the person as one
  line (the cause and the command that continues it) with a non-zero exit; the
  traceback goes to the log the hooks already report at the next session.
- **After a restart, queued work starts again.** Neither `cauce lanes
  --unpause` nor the end of a `cauce resume` started a dispatcher, so a task
  queued behind a stopped one stayed `queued` until a person ran `cauce work`
  by hand. Both now start the repository's dispatcher when work waits in its
  lane and none holds its lock (with `autowork` on, like the web board's
  unpause).
- **A lane paused on a task opens when that task is resumed.** A lane paused
  by "task #N stopped with no sign of life" stayed paused after #N was resumed
  and running again. A resume that starts the task now reopens a lane paused
  only on it; a pause on another task stays.

## [0.19.0] - 2026-10-16

### Added

- **Workspaces: a folder of repositories is one project.** Work that spans an
  API and the front end that shares its spec was tracked twice: two sets of
  notes (the same ones written twice), two boards, two lanes. `cauce workspace
  add <folder> --with <repo> --with <repo>` makes the folder that holds them
  (made when missing; a repository elsewhere is linked in; with no `--with`,
  the repositories right inside it) one project, its folder holding only its
  own repositories: every
  repository in it, and every worker's worktree of one, answers to the folder
  for its notes, tasks, lanes, sessions, team and kept rules, and a task that
  spans them runs in the folder, which holds every checkout in it (a linked
  one included). What each repository kept there moves to the folder (a clone
  elsewhere keeps its own, its notes copied), with the session names a person
  wrote, and a fact two of them kept is merged into one note with both one's
  anchors and links; a repository keeps its own project-scope workflows. A note about a file names its
  repository in front (`api/src/cart.ts`), so two files of the same name stay
  two, and a repository reviews only its own anchors. Declared in the cauce
  home, never in a repository. `cauce workspace list` and `rm` (new work is
  each repository's again). A task that reaches a sibling repository with
  `--add-dir` while they are tracked apart says so, with the command; cauce
  never makes a workspace for the person.
- **`cauce workflow run --dry-run`** checks the inputs and the memory, shows
  each step with the check it would run, and queues nothing.
- **`cauce workflow run --verify`**: the check of every step that writes and
  names none of its own (a reading step never gets it; a step with no kind
  gets it once it is classified into one that writes).

### Changed

- **A network that drops is waited for, not handed to the person.** A task
  whose session lost the API (`ENOTFOUND`, a connection error, an overload),
  whose worker found a name that does not resolve, or whose check's package
  manager or git could not reach the network, was blocked like a refused
  command. Now the same cell tries again after 30s, 90s and 270s, from the
  work it kept; those tries count toward no attempt ceiling (an unattended
  resume's included), and the waits are their own phase in `cauce stats`.
  Only a network that stays down stops it, as `network` (by the network, its
  work kept, resumed unattended once it is back). A local server that is not
  up, or a test that logs an unresolved name, is not the network.
- **A workflow's first start is the one that runs.** `cauce workflow list`
  shows each workflow's inputs and memory; text given without a name fills the
  one input not named with `--input`, and an error for several names them all
  in a command to copy. A memory topic with no note yet (a new project) no
  longer stops the run: it is said, and the step goes without it. A note or
  problem that cannot be read still stops it, with every problem at once.
- **A flag a command does not take says what was meant**: the near flag of its
  own (`--allw: did you mean --allow?`), the commands that take it (`--input
  is taken by cauce workflow run`), and how a workflow's inputs go.
- **`cauce workflow status` says how far each run got, in a line**: the step
  it is at, its attempt, what its worker is doing, its last commit and the
  files it touched; one run shows the same per step, its inputs on one line
  (`--full` shows them whole).
- **The servers notice only when it applies.** A task that mentions a dev
  server, and whose check needs none (lint, tests, a build), is no longer told
  to start one; one that tells the worker to start its servers still is. The
  notice names the words that set it off.

## [0.18.0] - 2026-10-16

### Changed

- **Cost is measured, never a brake.** A dollar cap stopped more good work than
  it saved: a worker cut off at its cap left half a change, and the next
  attempt started short. There is no `--budget` any more (on `cauce run`,
  `cauce resume`, `cauce queue add` and `cauce workflow`), no
  `--max-budget-usd` handed to a worker, and no attempt refused for the money
  left. What every attempt, check and run cost is still recorded and shown in
  the report, the board, `cauce stats` and the Analysis screen. Turns are the
  one dial of room: a task's are clamped to 200 (a workflow step's too: one an
  older cauce saved with more reads as 200), and attempts stay the ceiling of
  a run. A task an older cauce stopped on a budget reads back as such and
  resumes plainly; a workflow step's old `budget_usd` is dropped when the
  definition is read.
- **An unattended resume has a bound of its own** (`--unattended`: the mod's
  `resume` tool, and the resumes the orchestration guide now has a session
  start on its own): twice the attempts a run takes, beyond which it needs a
  person. An approval for Fable a person gave stays on the task for their own
  next resume; an unattended one never uses it, nor starts on a cell that
  needs it.
- **`cauce investigate` gives an estimate, not a cap**: the average cost of the
  investigators run before (when there are any) times the number of projects,
  in the dry run and when it starts. The init skill says so.
- **The bundled workflows are cut where context changes hands, never by
  role.** `feature`: build (the change with its tests) → review → refute → fix
  → recheck. `bugfix`: repair (a failing test first, then the fix) → review →
  refute → fix → recheck. `review-fix`: review → refute → fix → recheck, its
  review told that code with no change under review has nothing to excuse as
  pre-existing. A clean review ends `feature` and `bugfix` after two workers
  and `review-fix` after one; the ledger steps run only for the findings there
  are, and the refuter is told whether one review or two found them.
- **A review step of a change reads blind**: the change itself, never what the
  steps before said of it (a review with no change to read, such as a plan's
  critique, still gets what came before). A step that needs another gets its
  findings as records (where, what, evidence, cause, proof) beside its
  summary.
- **One agent first.** The orchestration guide starts from the session doing
  the work itself, and names what earns a worker (a context to keep clean, work
  to run beside it, another cell, an independent check). A judgment that needs
  the conversation goes to Claude Code's native fork (`/subtask`, the Agent
  `fork` type), never the check of the session's own work. The init skill, the
  `surveyor` and the investigation brief cut workflows by context and fold
  personas into roles.

### Added

- The independent verifier is told why it runs (the risk's reasons, to probe
  first), reproduces a bug at the base for a debugging task (in a worktree of
  its own, never by stashing the work it checks), and gets its own room (40
  turns at most, with a timeout to match).
- A task the check stopped for review resumes with what the check found, also
  after a dismissal: the blockers it stopped on (now kept on its `verified`
  event) seed the next round's ledger.
- What an attempt cost is split: the worker's own cost and the independent
  check's, and the prompt cache's reads and writes. `cauce stats` and the
  Analysis screen show what the checks cost and how much of the input the
  cache served, per cell and in all; an older run's checks are read from its
  `verified` events.
- How long filing notes takes is timed with the run's other phases.

### Fixed

- **A result block that quoted a fenced snippet, or held a raw tab in a
  string, lost the attempt its verdict.** The block's JSON is now decoded from
  where it opens, not cut at the next fence; two openers, or anything but the
  closing fence after its JSON (a second verdict), are still no verdict.
- A finding claimed as deterministic without its proof is read as inferential,
  so a refuter decides it instead of the claim.
- A reading worker is told where its answer and findings go in the result
  block.
- A worker no longer starts cauce work of its own: `cauce run`, `resume`,
  `work`, `queue add`, `workflow run`, `workflow retry` and `investigate start`
  refuse inside a worker (every reading command still answers), and a worker's
  Claude Code runs with forks off.
- An attempt's record keeps the start and the end of long evidence, so a
  resume's brief quotes the real last lines.
- A resumed investigator stays one: it still counts toward the estimate and
  stays out of insights.

## [0.17.0] - 2026-10-16

### Added

- **The scribe.** A worker started from the task's text alone, and a session
  that never compacted (to keep its thread) fed its project's notes only when
  it ended. At the end of every turn the Stop hook now reads what the session's
  transcript gained since the last time and keeps the facts of the session in
  the cauce home: the person's requests verbatim and which of them corrected
  the agent, the files it wrote, the calls that failed and the call that passed
  after them. No model reads or writes it and the session's context is never
  touched: it can leave things out to fit, never paraphrase. A failure of the
  scribe is noted and never costs the turn its endings.
- **Work a session sends gets its thread.** A task's brief carries the part of
  the sending session's thread it needs: its newest requests, the older ones
  that share the task's words, the files it wrote and the calls still failing
  there, verbatim and marked as context, not instructions. It is read up to the
  moment the task starts, so the turn that sent it is in. An investigation gets
  none. A `briefed` event records what went in.
- **Notes as the session goes.** The project's notes are filed every few turns
  (8 requests, or about 16k characters of conversation) by the detached Haiku
  that used to wait for a compaction or the session's end.
- **`cauce relay` and `/cauce:relay`**, the alternative to compacting: the next
  session that starts in the same place (`/clear`, or a new one) begins from the
  old session's thread — the requests in the person's words, the commits made,
  the files written, the calls still failing, the cauce tasks still open — and
  that open work, and the endings the old session never heard, report to the
  new session from then on. Taken once, never by a resume, dropped after a day.
- **Every run's phases are timed**, on its events: the wait in its lane, the
  planning, the setting up (worktree, waiting for the checkout, servers), each
  attempt's wall time, tokens and turns, cauce's `--verify`, the independent
  check (now with its duration and turns), and the run's wall time.
- **`cauce stats`** (`--repo`, `--days`, `--json`, `--runs [--csv]`) and the UI's
  **Analysis** screen: pass rate and first-try rate, time and cost per pass, by
  kind, cell, workflow and day; where the time went, phase by phase; how runs
  stopped and moved; a session's own turns and its subagents' delegations; runs
  with the session's thread in their brief beside runs without it; the slowest
  and costliest runs, and a JSON export. Read only, no model; a run recorded
  before the phases were timed counts by its timestamps and is left out of the
  breakdown rather than guessed.
- The `scribe` setting (plugin option, `cauce config scribe off`).

### Changed

- The orchestration guide says what a worker now sees of the session (the
  person's own words that relate to its task, never the session's reasoning),
  and offers the relay when a person wants a fresh start.
- Scribe states of sessions not seen for 30 days are forgotten at session start.

## [0.16.1] - 2026-10-16

### Fixed

- **`cauce survey` (and with it `/cauce:init` and `cauce investigate`) stopped
  on a plugin cache with a file in it.** `/plugin` clones a marketplace whole
  under `plugins/cache/temp_*` before it moves it in place, and a clone it
  never finished stays there: a repository's files, a `.gitignore` among them,
  at the level cauce reads plugins from. Listing the installed plugins opened
  that file as a folder and the survey exited before it read any session. The
  plugin cache is now read in one place (`plugins.versions`): only folders, at
  every level, and never a `temp_*` clone, for the plugin list, the neighbours
  cauce hands a worker and the skills a role names as `plugin:skill`. A clone
  of a neighbour left half done is no longer read as that neighbour.

## [0.16.0] - 2026-10-15

### Added

- **`cauce investigate`.** `/cauce:init` stopped at counts: two minutes of
  reading and a plan, with no branch opened, no chain of fixes followed and no
  pasted error matched to its fix. The deep reading is now cauce's own work:
  `cauce investigate start` queues one read-only worker per project (and one
  per plugin named with `--plugin`), each in its own lane so they run side by
  side, on `opus/high`, wearing the new internal `surveyor` role. Its brief
  carries the deep survey and lists what it must cover as criteria its pass
  has to answer: every chain of fixes and reverts explained from the commits,
  every unmerged branch accounted for, every reported error matched to its fix
  or marked unsolved, corrections turned into worker instructions, flows with
  their exact checks, every problem and fact with its evidence. A plugin's
  worker writes its phases and gates as cauce workflow definitions and its
  personas as roles. `--dry-run` shows what it would read and the most it can
  cost; `cauce investigate status --wait` says each ending, and `--json` prints
  every report. Nothing an investigator learned is filed: it waits on the person.
- **`cauce survey --deep`**: per project, every error the person pasted or
  quoted and what the session did after it (its answer, the files changed, a
  check run, a commit), a sample of their requests, every branch against the
  default one (merged, squash-merged or unmerged) with the commits that never
  landed and the fixes and security work among them, the chains of fixes and
  reverts that followed a change on the same files (a fix of a fix joins its
  chain: that fix did not hold), the reverts with what they undid, every fix
  with its files across all branches, cauce's kept branches that hold the same
  tree, and cauce's tasks that stopped with their reason.
- **Installed plugins** in `survey` (`have.plugins`): each one's skills,
  commands and agents, newest version, read from the plugin cache.
- **A worker's report.** A reading worker may return a `cauce-report` block
  beside its result; it is kept on the task as a `report` event.
- **Screenshots in the README**, taken from `cauce ui` on demo data, in light
  and dark, and a table of what cauce does at a glance.

### Changed

- **`/cauce:init` investigates before it proposes**: overview, then scope and
  cost agreed with the person, then `cauce investigate`, a check of every
  report (a stopped worker is resumed, never filled in by guess), then flows
  and failure families across projects, a plan with its evidence, and the
  build — memory first, so workflow steps can carry the problems it recorded.
  Plugins whose workflow the person relies on are taken into the team and the
  workflows. The README recommends running it on Opus at high effort.
- **Shell and tool noise is no problem.** A zsh parse error, a blocked
  `sleep`, an edit of text not found, a file not read first: `survey` counts
  them as `noise` and keeps them out of the errors and fixes it reports.
- `Options.keep_learned`: off, a passing worker's `learned` is filed neither
  in notes nor in engram.
- An investigator that stops never pauses its project's lane: the person's own
  queue there goes on.

- **`--service 'cmd@port'`** on `cauce run` and `cauce queue add`: cauce starts
  the server in the task's directory, waits for its port, runs the attempts and
  stops it after. 17 tasks of one machine had stopped on "this task needs
  running servers", most of them cancelled.
- **`--carry`** brings the person's uncommitted changes into the task's
  worktree; without it, a task that names an uncommitted file is warned before
  it starts. **`--add-dir`** gives the workers a sibling repository.
- **Ignored paths a task names are copied into its worktree** (up to 50 MB;
  never committed): a task about `scripts/slides/` found an empty folder.
- **`cauce tasks --turns`** shows the sessions' own turns and subagents; by
  default `cauce tasks` lists cauce's work only.

### Fixed

From a compendium of 1,207 real tasks:

- **A session's turns showed up as interrupted tasks to resume.** 67 chat
  messages ended `interrupted: no sign of life` with "resume it", and the
  endings notice the mod submits to wake a session became a task of its own
  (one even `failed`). A turn that ends without its answer is now a `turn`
  stop with nothing to resume, cauce's own notices are no turn, and
  `cauce tasks` leaves turns out.
- **Running out of turns read as a bug in the code.** When the worker ran out
  of turns, its check passed in its place and the independent check then found
  a problem, the next attempt climbed effort and model (sonnet/high → xhigh →
  opus, about $6.50 on one task), and "two cells gave the same answer" fired on
  the wrong cause. It now gets the same cell with twice the turns, and is no
  dead end in memory.
- **A timeout after real work was "the environment".** A feature that changed
  24 files and ran past 1800 s was retried once and then blocked as
  "environment failed twice". A timeout after changing files is now out of
  turns: it goes on with more turns from what it did, and an attempt's time
  grows with its turns (60 s per turn, never under 1800 s).
- **UI and test work start with 60 turns**, as features do: at 30 they ran out
  before a verdict more than any other kind.
- **A new model looked for work that had been reset away.** After a wrong
  approach the worktree goes back to the base; the next brief now says so, and
  what the last attempt wrote is kept on `refs/cauce/aside/task-<id>-<n>`.
- **Workers sourced nvm and were refused.** The brief now names the Node already
  on the worker's PATH, forbids `source ~/.nvm/nvm.sh`, `nvm use` and
  `bash -c`, and lists the shell commands the person's rules let through.
- **An attempt was launched with $0.03 left on opus/xhigh.** After the first,
  no attempt starts with less than its model can use (haiku $0.03, sonnet
  $0.20, opus $0.50, fable $1): the task stops as `budget` with the resume
  command.
- **Criteria the request put out of scope failed the pass.** Items under a line
  like "Part B (do not touch):" became S5 and S6, and a pass that left them
  alone was a `spec_bug`. They are now no criteria, and the brief names them as
  out of scope.
- **A livespec index that cannot be built was retried by every task.** A
  failure is kept and not tried again until the checkout moves.
- **Duplicate `cauce/task-*` branches.** `cauce prune` deletes a finished
  task's branch when another kept branch holds the same tree, and a task asked
  again while one with the same text keeps its work points at `cauce resume`.
- **The task drawer's attempts were squeezed into 140 px.** The map's
  milestones list shared the `.timeline` class, and its grid clipped every
  attempt's cell, turns and cost. The milestones have their own class now.

## [0.15.0] - 2026-10-15

### Added

- **`/cauce:init`**, which replaces `/cauce:personal-team`. It reads every
  Claude Code session on the machine and the history of every repository they
  ran in, finds the flows the person's work follows, and — once they agree —
  builds one team covering every flow, one workflow per flow with measurable
  criteria, and the memory of what was already solved: project notes and the
  problems with the fixes that worked. It writes only through `cauce roles`,
  `cauce team`, `cauce workflow`, `cauce note` and `cauce memory record`.
- **`cauce survey`** (`--json`, `--repo`, `--days`, `--sessions`,
  `--per-project`): read only, no model. Per project: what the person asks for
  and the shape each session takes (`plan → implement → check → commit → pr`);
  friction (corrections with what the agent said before them, interrupts,
  refused calls, bare "go on"s, pasted logs); errors solved in-session, with
  the files changed between and what the agent said before the pass, and the
  ones never solved; the commands and checks run, skills, subagents and slash
  commands used; and the repository's history: cadence, commit types, fix
  share, reverts, merges, branch prefixes, agent share, hot and fragile files,
  recent fix subjects. Across projects, each session shape is counted with the
  projects that show it, never quoted. A cauce worker's worktree, the home
  directory and `/` are no project's.

### Changed

- **Commits read as the change.** A worker proposes the commit's subject
  (`commit` in its result block); the kept commit is that line (else the task's
  title), the worker's summary under it, and the task in trailers:
  `Cauce-Task: #12`, `Cauce-Verified: no (blocked)` while unverified. A
  Before, a merged branch read as `cauce task #1144: Repo wapp-admin…` in the history.
- **The independent check no longer stops work that is getting better.**
  Fixing every problem the round before found is progress and earns the next
  round (two corrections at most, as before) when a probe shows a new problem;
  a new problem after progress that is only read from the code goes with the
  pass, recorded on the `verified` event and in the task's findings. Before,
  any new finding after the first correction stopped the task as `review`:
  a palette change was blocked with each round fixing what the last found.
- **The verifier measures, and is told to.** It keeps the neighbours that only
  measure (layout-inspector) when the work had them, and its instructions say
  to report everything on the first look, to measure UI findings, and that
  taste is no finding.
- **The verifier runs at high effort** on the work's model (never Haiku):
  work that climbed to xhigh or max was checked at that effort too, doubling
  the wait on the slowest tasks. Its independence is the fresh session given
  the request verbatim.

## [0.14.2] - 2026-10-13

### Fixed

- **Subagents' turns are counted.** A subagent writes its own transcript
  under `<session>/subagents/`, and only the session's file was read: in a
  real store not one of 13,832 usage records was a subagent's, and a session
  that delegated its building showed about 60% of what it cost. `Stop` now
  reads those transcripts too, each counted to the delegation its tool call
  opened (from the `.meta.json` beside it).
- **A subagent's report is no task.** Claude Code hands a background
  subagent's final report back as a turn (`<agent-message …> [Subagent
  hand-back]`), and each became a task of its own: four of fifteen in one
  session. It now joins the task that delegated the work, which records the
  answer the session gives on it.

### Added

- The task and session views price a session's own turns (`turns`, its
  subagents included) apart from cauce's workers (`workers`). A prompt
  answered in a session showed $0.00 whatever it cost.

## [0.14.1] - 2026-10-13

### Changed

- **A prototype is functional work.** The `frontend` role and the `ponytail`
  skill tell a prototype (a mockup, a wireframe, "just make it work") from a
  product screen:
  - a prototype gets every state and semantic markup, with the fewest neutral
    styles, each marked `ponytail: prototype styles`, and never an invented
    palette;
  - a design system or brief that exists is applied in full;
  - only a product screen with neither still stops to ask.

## [0.14.0] - 2026-10-13

### Added

- **engram, adopted** (`adapters/engram.py`). For teams whose agents already
  keep their memory in engram:
  - the brief recalls the project's engram memories as data;
  - workers get `engram mcp` read-only, behind `mcp_guard`'s new `engram`
    policy (reading tools only, held to the project);
  - with `engram_write` on (off by default), what a passing worker learned is
    saved as engram observations, one session per task, each save with an
    operation id so a retry never saves twice.

  cauce talks only to engram's server (HTTP or Unix socket, bearer token
  honoured), after `/health` and `/compatibility` verify it, and never opens
  its store. New settings: `engram` and `engram_write`.
- **`<private>…</private>`**: text a person marks private never becomes a
  note and never reaches engram.

## [0.13.0] - 2026-10-13

### Added

- **A review ledger with a refuter.** Workflow steps take `"ledger"`:
  `refute`, `fix` or `recheck` (`--ledger` on `cauce workflow step`). The
  engine builds the ledger `F1..Fn` from the blocking findings of the reviews
  before, frozen with a sha256. Reviewers do not vote: one is enough to put a
  finding on it.
  - A row only one reviewer saw and could only read from the code goes to a
    refuter. It answers `confirmed`, `refuted` or `unsure` with its own proof;
    `unsure` comes to the person.
  - The fix gets only the confirmed rows. The re-check answers `fixed`,
    `not_fixed` or `regression` for each row against the fix's own diff.
  - A row still open gets a second round; after two, the run stops as
    `review`. A ledger step with nothing to decide passes without a worker.
- **`verdicts`** in the result contract: a pass that leaves a given row
  without a valid verdict is `inconclusive`.

### Changed

- **`dual-review`** now runs refute → fix → recheck, with up to two rounds.
- A workflow step that passes while a sibling still runs is no longer handed
  to the session as an ending.

## [0.12.0] - 2026-10-13

### Added

- **Criteria.** A request that lists what it wants (bullets, numbered lines)
  becomes criteria `S1..Sn`, kept verbatim in every brief. A pass answers each
  one with its evidence; one left out is `inconclusive`, one not met a
  `code_bug`.
- **Findings as records.** Reviewers, auditors and the verifier report
  `where`, `what`, `evidence_class` (deterministic, inferential, insufficient)
  and `cause` (introduced, worsened, pre-existing, unknown). Only the change's
  own, shown findings block; two attempts naming the same places converge.
- **Risk tiers.** Each task has a tier (passive, medium, high) read from its
  request, kind and changed files, with its reasons, only ever raised; shown in
  the report and the UI.
- **An independent verifier.** A high-risk pass, or a medium one from a cheap
  cell, is checked by another cell, read-only, given the request and the diff
  but never the worker's summary. A blocker that persists earns at most two
  correction rounds; new findings alone stop the task as `review` for a
  person. `cauce config verifier off` turns it off.
- **Edit surfaces.** `--surface <glob>` (run, route, queue add) and a workflow
  step's `surfaces` bound where a task writes. A change outside them fails the
  attempt and is undone in its worktree. Tasks with disjoint surfaces run side
  by side without asking Haiku.
- **`dual-review` workflow template**: two blind reviews on different models,
  a fix of only the findings the change caused, and a re-check.
- **Phase.** A running task shows whether it is exploring, implementing or
  checking, on the board and in the mod's status line.
- **The change in the drawer.** A task's drawer shows its risk, its
  independent checks and, on demand, its diff (`GET /api/tasks/<id>/diff`).

Ideas adapted from gentle-shell (MIT).

## [0.11.0] - 2026-10-12

### Added

- **`/cauce:personal-team`**, the plugin's first skill. It reads how the person
  works and proposes one team per purpose: several sessions on several
  projects usually need several teams. Once the person agrees, it builds the
  roles on cauce's own, creates the teams and gives each project its team.
- **`cauce team insights`** (`--json`): every project cauce worked in, with:
  - the kinds asked for and how each went (pass rate, where it passed, how it
    stopped, what it cost);
  - the failures of attempts that did not pass;
  - its languages by share of files, its manifests, whether it has a UI;
  - the checks its sessions repeat after an edit, and the team it uses;
  - suggestions (a language specialization, a kind that passes badly, kinds
    never asked for).

  Beside it: the person's roles, teams and Claude Code subagents, by name and
  description. Read from what cauce kept and from the checkouts, never by a
  model.
- **`cauce roles new <name> --extends <role>`**: a role built on another, with
  `--match`, `--skill`, `--on-demand`, `--capability`, `--disallow`, `--kind`
  and `--prompt` (`-` reads stdin). It keeps the parent's instructions and
  follows them; its lists are the parent's plus the ones given.

- **A workflow step names its role** (`"role": "security-auditor"`,
  `cauce workflow step add … --role`). It is used only when the project's
  team has it: a definition a repository commits never chooses beyond the
  person's team.
- **rastro behind a guard.** `cauce.mcp_guard` sits between a worker and
  rastro and refuses, before they reach it, a browser profile, a saved routine
  or the profile list, and writes or uploads to any host but localhost; every
  call runs in rastro's session `cauce-worker`. The hint asked for this; the
  guard enforces it.
- **`plugins` on the plugin's settings screen**, beside the other settings;
  a test holds every setting to an option there.
- **Roles that add to their parent's lists.** `add_skills`, `add_on_demand`,
  `add_capabilities`, `add_disallowed_tools` and `add_match` extend the
  parent's list instead of replacing it, so a derived role follows its parent
  as it changes. `cauce roles new` writes them, and a role it makes no longer
  freezes its parent's kinds.

### Fixed

- A skill a role carries whole is no longer also offered by path.

## [0.10.0] - 2026-10-12

### Added

- **Installed neighbours as capabilities.** layout-inspector and rastro,
  installed as Claude Code plugins (or on PATH), reach workers without a
  `capabilities.json` entry:
  - layout-inspector for `ui` work, and after a failed `implement` or
    `feature`;
  - rastro for `test` work, and after a failed `ui`, run with its saved
    routines off.

  The server entry is read from the newest installed version, from the
  manifest, the file it names or `.mcp.json`, with `${CLAUDE_PLUGIN_ROOT}`
  filled in. Each comes with its plugin's skill to read first and a read rule
  for its folder. A registry entry of the same name wins; `cauce config
  plugins off` leaves them out. `cauce capabilities` and `cauce neighbours`
  say where each comes from.
- The `frontend` role names rastro beside layout-inspector, and `tester`
  names rastro.

## [0.9.0] - 2026-10-12

### Added

- **Roles.** Every worker runs as a role: a name, what it is for, the kinds it
  takes, its instructions and the skills it carries, all in the worker's
  system prompt beside Claude Code's own. cauce still picks the model, effort
  and tools of every attempt: a role names no model and adds no tool, it may
  only take tools away, carry skills (`skills`), point at them (`on_demand`)
  and name the person's capabilities. `extends` builds a role on another;
  `match` makes it a specialization picked where the project holds its files.
- **The bundled team**, one role for every kind: `answerer`, `researcher`,
  `docs-writer`, `tester`, `refactorer`, `implementer`, `frontend`,
  `feature-builder`, `debugger`, `investigator`, `reviewer`,
  `security-auditor`, `architect`, `frontier`. Most adapt vise's agent
  charters; `security-auditor` carries Cloudflare's security-audit skill.
- **Bundled skills**, credited with their licenses in
  `src/cauce/skills/README.md`: vise's engineering-baseline, ponytail,
  security-baseline, architecture, ui-critique, web-ui-rules, design-brief and
  fourteen language rules (MIT), and Cloudflare's security-audit (MIT).
- **Teams.** A person keeps several teams and gives each project one (`cauce
  team create`, `cauce team use`); a project's workers run only as its team's
  roles, and its tasks are classified only into the kinds that team covers.
  Teams, roles and which project uses which live in the person's cauce home.
- **`cauce roles`** (`list`, `show --full`, `copy`, `rm`, `check`, `skills`)
  and **`cauce team`** (`list`, `show`, `create`, `change`, `delete`, `use`);
  `cauce run --role <name>` runs a task as one role.
- **Teams screen** in `cauce ui`: every team, the roles in it with their
  kinds, skills, capabilities, tools taken away and instructions, and the
  projects using it (`GET /api/teams`).

### Changed

- **Color you read without reading.** The web UI gives every piece of state
  one tone, always beside its word:
  - a task's state is a badge with an icon (✓ done, ● running, ○ queued,
    ! blocked, ◆ needs approval, ✕ failed);
  - the kind of work is a colored dot by family (read, debug, test, docs, ui,
    build, plan, review): eight hues in a fixed order, checked for colour-blind
    separation and contrast in both themes;
  - a cell shows its model as depth (haiku light to fable dark) and its effort
    as five pips;
  - board columns, cards that wait on a person or failed, ladders, the routing
    pass rates and role cards wear the same tones.
- The mod's status entry and its toast carry the same tones as markers
  (🟠 waits on you, 🔵 running, ⚪ queued; ✅ passed, ❌ failed), and the toast
  says how each task ended: `cauce endings --json` gives each one's status.
- A system prompt over 16,000 characters goes to the worker in a file
  (`--append-system-prompt-file`).

## [0.8.0] - 2026-10-11

### Added

- **Workflows screen.** `cauce ui` draws each workflow run of the project as a
  graph of its steps:
  - columns by what each step waits on;
  - every step with its state, cell and tries, opening its task;
  - the run's progress and the branch holding its work so far, and what to do
    when a step waits.

  Below are the workflows the project can run, each with its version, steps,
  step memory, the runs holding it and any change waiting for them.
- **Map screen.** A mind map of the project, built from what cauce keeps
  without a model: topics → live notes → the code each is about, and problems
  → every fix tried. The typed links between notes are dashed cross-edges. A
  topic or problem folds, a filter prunes the tree, and a picked note shows
  its text and links. **Milestones** lists, newest first, the workflow runs
  that finished, the decisions the notes keep, and the features, refactors and
  plans that passed.
- **Attach a note to a step from the map.** A person's press carries a note
  into a workflow step's memory (`POST /api/workflows/attach`, token
  required). A workflow in use gets the change as a pending revision.
- `GET /api/workflows`, `/api/map` and `/api/milestones` read one project.
  SVG is drawn node by node with `createElementNS` and `textContent`, never
  markup, and the map keeps its folds and filter when live events arrive.

## [0.7.0] - 2026-10-09

### Added

- **Workflows.** A workflow is a saved chain of tasks that goes on by itself.
  A run queues its first steps. When a step passes, the engine queues the
  steps that waited on it and starts the dispatcher, with no session relaying
  anything:
  - a writing step starts its worktree from the commit the step before it
    kept;
  - a reading step (a review, a plan) reads that work in a worktree its tools
    cannot write to;
  - every brief says what the earlier steps found.

  Each step is an ordinary cauce task with its own cell, check, escalation
  and stops. A step that stops stops the run there, and a resume to a pass or
  `cauce workflow retry` goes on. The session that started the run hears of
  it once, at its end or when a step waits on the person.
- **Definitions** are JSON in three scopes, the nearest winning: the project
  (`.cauce/workflows/`), the user (the cauce home) and the bundled templates
  `feature`, `bugfix` and `review-fix`. `cauce workflow list|show|new
  [--from]|copy|save|edit|rm|validate` and `step add|set|rm` make and change
  them, and every change is checked before it is written. Changing a bundled
  template copies it to the user scope first. A definition holds no
  permission rule, approval or capability: those are flags of `cauce workflow
  run`.
- **Global workflows are held while in use.** A user-scope workflow is one
  definition reused by several projects, and each run keeps its state with
  its own project. Every definition has a version, and a run runs the copy
  and version it started with. While any active run uses a definition:
  - a change becomes a pending revision, and later changes build on it;
  - the revision applies by itself when the last such run ends or is
    cancelled (`cauce workflow pending [--apply|--drop]`, `--now` to write
    at once);
  - removing it needs `--force`.
- **Memory per step.** A step can carry cauce memory into its brief: `note:<id>`
  (a replaced note is read as the one that replaced it), `topic:<name>` (every
  live note in it) and `problem:<id>` (with every fix tried on it). Notes are
  read only from the run's own project. A run whose memory cannot be read
  there does not start. Set it with `cauce workflow step add|set --memory`.
- **Watching:** `cauce workflow status [<run>]` shows each step's state, task
  and cell, what runs now, what waits on the person and the branch so far.
  `cauce overview` lists the runs and names each step's run. `cauce workflow
  cancel` stops a run.
- **Guidance:** `/cauce:workflow` takes a session through all of it, and
  `/cauce:orchestration` now sends a sequence of phases to a workflow instead
  of relaying it by hand.

## [0.6.21] - 2026-10-09

### Added

- **A watcher outside cauce reads only its repositories.** `cauce overview
  --repo DIR` and `cauce events --repo DIR` (both repeatable) keep those
  repositories' sessions, tasks and events; `cauce events --last-id` prints
  where a watcher starts, and each event `--json` prints carries its task's
  title, repository and session. A page that shows some repositories (Tanka's)
  no longer reads every one to filter them itself.
- **`cauce resume <id> --pressed`.** A person's press on another page runs the
  same resume the UI's button does, read from cauce's account of the stop
  (`stops.resume_args`); a rule, approval, budget or `--keep` passed with it
  is refused. `cauce dismiss --via tanka` says the dismissal came from Tanka's
  page.

## [0.6.20] - 2026-10-09

### Changed

- **Habits are per project.** Every repository's tool events were mined
  together, so a Python repository was offered twenty TypeScript habits and
  none of its own, and a count summed over unrelated projects passed the gates
  for each of them. `cauce habits` (and the UI's habits screen, and
  `/cauce:habits`) now mine the current repository's own sessions and workers,
  gated on them alone, and list apart the habits two or more other
  repositories share that this one has not shown yet. `--all` mines
  everything together, as before. An event's repository comes from its task,
  else its session (99% of a real store's events have one); the PostToolUse
  fast path still writes no path.
- A bare interpreter run (`bash:python3`, `bash:node`: a script or a `-c`
  one-off) is no habit: `python -m pytest` is already `bash:pytest`. After a
  `.ts` write it was the model's scratch work, and the top candidate.

## [0.6.19] - 2026-10-09

### Added

- **`/cauce:habits`.** One skill reviews the habit candidates for the
  repository it runs in and installs the ones the person picks: it keeps those
  whose file type the repository has, whose last step is a fast check it
  really has (the exact command read from its own files), that pass now and
  are not installed yet; it offers to remove habits that turned themselves off
  or are installed twice. `cauce habits list --json` and
  `cauce habits status --json` (with where each habit lives) are what it reads.

### Fixed

- **A failed habit is said to the session.** A habit ran its check after each
  edit and kept the result to itself, so the session ran the check again by
  hand and the habit saved nothing. A failure now exits 2 with the end of the
  output, which Claude Code shows the session; a pass says nothing.
- **A habit is installed once.** The same command on the same file type in one
  settings file was installed twice, and ran twice after every edit.
  `cauce habits install` now says it is already there.

## [0.6.18] - 2026-10-09

### Fixed

- `cauce events` without `--follow` printed the first 200 events and stopped,
  so `--kind branch_pruned` found nothing in a store with more. It reads every
  page now.

## [0.6.17] - 2026-10-09

### Fixed

- **A typed command runs the newest cauce installed.** A session's PATH keeps
  the plugin version it started with, and `/reload-plugins` does not change
  it: one update later, `cauce prune` was an invalid choice. A command now
  forwards, once, to the newest install in Claude Code's plugin cache. Hooks
  stay on the version that registered them, and a copy outside the cache (a
  checkout) never forwards.

## [0.6.16] - 2026-10-09

### Added

- **Branches whose work landed are deleted.** A `cauce/task-N` branch of a
  task that is done (or dismissed) goes once nothing would be lost: its
  commits are on another branch (a merge), HEAD holds every file it changed as
  it changed them (a squash or a cherry-pick), or all it changed is tool
  artifacts. A branch checked out in a worktree, or of a task that may still
  resume, stays. Each deletion is a `branch_pruned` event on the task with its
  tip, so `git branch cauce/task-N <tip>` brings it back. It runs at a
  session's start for its checkout and every ten minutes in the UI server;
  `cauce prune [--dry-run] [--repo DIR]` runs it by hand, and the `prune`
  setting (on by default) turns it off.

## [0.6.15] - 2026-10-09

### Added

- **One session watches the others.** `/cauce:watch` makes a session the
  watcher of every session that uses cauce, one per project: `cauce overview`
  (and the mod's `overview` tool) lists every session seen lately with its
  directory, last prompt and work: what waits on the person and why, what runs
  on which cell and since when, what is queued, what ended. It reads and
  claims nothing, so each session still hears of its own endings. Sending a
  session work is Claude Code's `ListAgents` and `SendMessage`.
- `cauce events --kind <kind>` keeps one kind of event, and `--new` starts
  from now: `cauce events --follow --new --kind finished` is every project's
  endings as they happen.

### Fixed

- The README described the mod's band and pane, gone since 0.6.12.

## [0.6.14] - 2026-10-09

### Fixed

- **A task that builds something read-only still writes.** Since 0.6.10, a
  task whose text said "read-only" anywhere was briefed as one: a feature
  asking for a read-only version history ("historial de versiones solo
  lectura") was told it could change nothing, three times, and each stopped as
  a task found wrong; a docs task saying "do not change code" could not edit
  its README. The words must now be about the task ("READ-ONLY VALIDATION",
  "(read-only)", "do not edit any file"), a bound like "do not edit any file
  under src/admin" is no such declaration, and `feature`, `implement`,
  `refactor` and `docs` tasks are never read as read-only.
- **No review card for a branch of tool artifacts.** Validations committed
  before 0.6.10 left branches holding only a browser's logs and snapshots;
  the board asked for each to be reviewed and merged. A kept branch whose
  changed files are all `isolate.ARTIFACT_DIRS` waits on nobody.

## [0.6.13] - 2026-10-09

### Changed

- **Haiku 5.5.** Haiku has the effort dial now (low to max, default medium)
  and costs a tenth of what Haiku 4.5 did ($0.10 / $0.50 per MTok), so the
  matrix gives it its dial and its rates: `classify` climbs haiku/low →
  haiku/medium → sonnet/low, `explore` and `docs` haiku/medium → haiku/high →
  sonnet/medium. A cell older runs recorded as plain `haiku` reads as
  haiku/medium, the model's default. cauce's own questions to Haiku
  (classifying, dispatch, notes, session names) ask at `--effort low`.
  Spend is priced at the new rates. A real `explore` task in a sandbox
  passed at haiku/medium on claude-haiku-5-5 for $0.01.
- Pin Haiku with `cauce config model haiku claude-haiku-5-5` to keep workers
  on it whatever the CLI's alias says.

## [0.6.12] - 2026-10-09

### Fixed

- **A lane paused on a task opens when that task leaves the board.** A queued
  task waited 16 hours behind a lane paused on a task the person had already
  dismissed. Dismissing, superseding, or resuming the stopped task to a pass
  unpauses the lane and starts the queue again. A session's ending says when
  a lane is paused on it and how many tasks wait, and the guide tells the
  orchestrator to unpause it when the queued work does not depend on it.

### Changed

- **The board lives in the web UI only.** The mod's band above the prompt
  and its `/cauce` pane were a poorer copy of the web board: they are gone.
  The mod keeps its tools, waking the session on endings, and a status line
  in words; `/cauce` starts the web board. The task drawer gains Resume,
  Allow & resume, Approve & resume and Always allow here; the rules a press
  resumes with come from cauce's account of the stop, never from the page.

## [0.6.11] - 2026-10-09

### Fixed

- `--kind research` (and `audit`, `fix`, `validation`…) is read as the kind it
  means, said on stderr; a word that means none is a usage error that lists
  the kinds and says cauce classifies when `--kind` is left out. A session
  guessed a kind, the run never started, and the background pipe hid it.
- The orchestration guide names the kinds, says to leave `--kind` out, never
  to pipe a run through `tail`, and to read `task #N started` before telling
  the person a run is going.

## [0.6.10] - 2026-10-08

### Fixed

- **A validation that says it is read-only is read as one.** A task whose own
  text says "read-only", "solo lectura" or "do not edit files" gets the
  reading brief (its findings are the answer, broken code included), and two
  cells that changed nothing and found the same thing stop as `converged`
  instead of climbing to a stronger model. It does not hold the checkout.
- **Tool artifacts are no work.** Files a worker's tools write for themselves
  (`.playwright-mcp/` page snapshots and console logs, `__pycache__/`) are never a changed
  file and never committed to a kept branch: they made read-only tasks look
  like writing ones.
- **"needs running servers" is said only when it is true.** A task that names
  its servers by `localhost:<port>`, all of them answering, is not warned.
- A run waiting for the checkout says how to work beside it (a worktree) and
  that a wait is not a hang.
- The compaction digest leaves out what the person never typed: a prompt a
  plugin submitted, a bare slash command, "[Request interrupted…]".

### Changed

- The orchestration guide: never send copies of one task to compare
  answers; parallel runs that share state a run uses up (a demo user, its
  attempts) spoil each other; never cancel a run because it waits.

## [0.6.9] - 2026-10-07

### Added

- **cauce compact.** Before a compaction (`/compact` or automatic), cauce reads
  the session's transcript and git and keeps a digest of facts: the person's
  requests verbatim, the cauce tasks of the session, the calls that failed
  with their exact error, the files git shows changed and the files the
  session wrote. The SessionStart after the compaction puts it back beside
  Claude Code's own summary, which paraphrases from memory. No model call,
  so it invents nothing; it fits a budget, newest first, and requests never
  take more than half of it. It runs in any directory, notes setting or not.

## [0.6.8] - 2026-10-07

### Changed

- **A new look for `cauce ui`.** The default blue and grey gave way to the
  palette of a riverbed: wet stone for the ground, deep water for what
  moves, ochre for what waits on you, iron oxide for what stopped. Every
  text colour meets 4.5:1 against the surfaces it sits on, in both themes.
  One typeface (Avenir Next, Segoe UI Variable, then the system's);
  monospace only for real code. Cards carry a stripe in their status's
  colour; "Needs you" is the one lane with an ochre count; tabs are
  underlined; labels are sentence case; the brand mark is the channel
  itself. Task text and worker accounts read as prose, command output as
  code. Destructive buttons stay quiet until hovered.
- Switching to a view says "Loading…" at once: Spend reads every session's
  transcript and could take seconds with no sign the click landed.

## [0.6.7] - 2026-10-07

### Changed

- **An environment the worker named blocks at once.** Of 18 retries after an
  `environment` failure, none passed: 16 were the worker's own diagnosis (an
  old Node, a server it could not start, a missing CLI), which the same cell
  meets again at once. The one retry is now kept for a launch that broke (a
  timeout, no JSON from the CLI, an errored session); a worker's own
  `environment` verdict, or cauce's check that could not run, blocks with its
  account.
- **A feature starts with 60 turns.** Its attempts used 29.7 of 30 turns on
  average and a quarter ran out, each costing a fresh worker's orientation;
  the raised ones passed at 31 to 45. Other kinds keep 30, and `--max-turns`
  still wins. The plan says so in its reasons.
- **Routing has a period** (last 7 days by default, 30, or everything, kept
  per viewer): a rule fixed last week no longer shows as a rung that fails.
  A move that goes on but has no next attempt says why (`running`,
  `cancelled`) instead of "stopped".

## [0.6.6] - 2026-10-07

### Fixed

- **A refused write the worker got past blocked the task anyway.** Auto mode
  refused two `Write` calls in a task's own worktree; the worker wrote both
  files afterwards, and git shows them on its branch. cauce still read the
  refusal as a setting, blocked the task, and asked a person to allow two
  absolute paths in a temporary worktree, where the same attempt, out of
  turns, would otherwise have gone on with more. A refused write to a file
  in the worker's own directory that git shows changed after all is now no
  refusal: the attempt moves as its failure says, and the `attempt_finished`
  event lists it as `refused_but_written`. A refusal of anything else, or of
  a file the attempt never wrote, still blocks.
- A test of the checkout lock released the lock on a timer and could pass
  without the run ever waiting; it now releases when the run says it waits.

## [0.6.5] - 2026-10-07

### Fixed

- **A dismissal reached the session as an ending.** Dismissing (or a pass
  superseding) wrote a new `finished` event, so the Stop hook handed the
  session that sent the task "#192 ended dismissed" and blocked its turn's
  end. The person did it, or the pass's report already names it: both are
  now marked reported as they are written.
- **A run in a folder that is no checkout did not hold the checkouts inside
  it.** A run in a parent folder and one in a repository inside it worked at
  once. A run in such a folder now holds the folder and every checkout right
  inside it, in one sorted order.
- **A dismissed task that had been replanned offered `cauce resume`,** which
  refuses it. It now offers nothing, unless it had only outgrown its turns.

## [0.6.4] - 2026-10-07

### Added

- **`cauce dismiss <id>…`** takes a task that waits on you (blocked, replan,
  failed, needs approval, interrupted) off the board. Until now only a resume
  did, and a stale audit stayed in "Needs you" for good. Its attempts and
  result stay; the account says you did it and what it had stopped on, and
  `cauce resume` brings it back as what it was (a task found wrong is still
  rewritten, never resumed). A **Dismiss** button in the UI's task view, the
  session's cauce band and the `/cauce` pane does the same.
- **A pass takes its stopped copies off the board.** Earlier tasks with the
  same text in the same repository that stopped short of a pass are dismissed
  as `superseded`, naming the task that passed; its report lists them.
- **`cauce memory forget <id>…`** and a **Forget** button in the UI's Memory
  view: a problem that was never the work's (the environment, two runs in one
  checkout) leaves memory with its fixes, and no brief shows it again.

### Fixed

- **Hide in the session's band brought every card back** as soon as any one
  changed: it remembered the whole list at once. It now puts each card away
  on its own; a card comes back only when its own status changes.
- **"Done" grouped by day but listed by id**, so a task resumed today showed
  under an older day. It is now ordered by when each task ended.
- **`--start haiku/low` crashed with a traceback** mid-run. A cell with no
  such dial is now a usage error, said before anything runs.

## [0.6.3] - 2026-10-07

### Fixed

- **A session kept running the cauce it started with after an update.**
  Its Bash put the old version's `bin/` first on the PATH, so its runs went
  on without the update's fixes: an environment failure that had changed ten
  files was retried, a rule 0.6.2 had already fixed. `cauce run` and
  `cauce resume` now say at once when a newer cauce is installed, and ask for
  `/reload-plugins`.
- **Two runs without a worktree worked in one checkout at once.** One
  reinstalled `node_modules` while the other's check ran, and the check
  failed with `Cannot find module 'vite'`: a `code_bug` that was neither
  task's work, and a climb to more effort. A run in the checkout itself (no
  worktree, and it writes or has a check) now holds it: a second one waits,
  says which task it waits for, and still stops on a cancel.
- **An errored session said "the session errored: success".** An API error,
  such as a usage limit, arrives with subtype `success`; the reason is in the
  result text, and that is what the summary now shows.

## [0.6.2] - 2026-10-06

### Fixed

- **A worker ran an older Node than the project's tools need.** The project
  declared no Node, but one of its dependencies did: a CLI with
  `engines.node >=22.22.3`. The worker got the PATH's 22.16, and every `npm
  test` failed before running. When a project declares no Node, cauce now
  reads the `engines.node` of its installed direct dependencies. When the Node
  on the PATH fails them, it puts first an installed Node that fits, of the
  same major when there is one, so native modules still load. When none fits,
  the plan warns at once, naming the dependency and the version. A spec cauce
  cannot read, a dependency that is not installed, or an unknown current Node
  changes nothing.
- **An environment failure was retried after the work had already run.** The
  retry met the same wall. An environment failure on an attempt that changed
  files now blocks at once, and the work is kept on its branch. Work that
  never ran is still retried once.
- **A check that could not run counted as a bug in the work.** Examples: a
  missing npm script, a missing `package.json`, a tool's minimum Node, or
  exit 127. The task climbed to stronger models against a wall none of them
  could pass, and the failure was kept as a dead end. Such a check is now
  `environment`. A failing test that only quotes such text, or that runs
  after npm's EBADENGINE warning, is still a bug in the work.
- **Unrelated failures reached workers as dead ends.** The brief matched any
  word of the task, so tasks in the same repository that shared a word or two
  showed up as if they were this problem's. It now matches as strictly as the
  prompt hook does.
- **A resumed task numbered its attempts from 1 again** in its progress lines
  and its `attempt_started` events. They now go on from the task's last
  attempt.

## [0.6.1] - 2026-10-06

### Fixed

- **A run in a background shell said nothing for minutes, and had no task to
  show.** A stale or missing livespec index was rebuilt before the task was
  created, and on a large repository that took minutes. The index now
  builds in the background, under the same lock as every other refresh. The
  task starts at once: it uses the index as it is, or runs without one. A
  repository livespec cannot index says so, instead of claiming a refresh is
  running.
- **`cauce run` and `cauce resume` printed nothing until the end.** They now
  print progress to stderr as it happens: the task number, warnings, each
  attempt and how it ended. The report on stdout stays the same. A warning
  that a task needs a browser or running servers now shows before any money
  is spent, not only in the final report.
- **The mod's tools answered "Invalid tool parameters" to a value outside a
  list.** This happened with a kind cauce does not have, a topic alias, or an
  id written as `"42"`. The schemas no longer constrain these values; the
  handler checks them instead and says what it accepts:
  - an unknown kind is dropped and cauce classifies the task;
  - cauce resolves a topic, aliases included, and an unknown one comes back
    with the project's topics;
  - `note` without a topic is filed by its words;
  - links it cannot read are named in the answer.

### Added

- **Fewer approvals for the same thing.**
  - `cauce resume <id> --allow <rule> --keep` also keeps the rules for every
    task in the task's repository, so the next task there is not refused the
    same program. It is refused together with `--unattended`.
  - The mod's band and pane offer **Always allow here** next to *Allow &
    resume*.
  - The mod's reading tools (`recall`, `tasks`, `note`) are never put to the
    person. A rule or setting that denies them still does. `queue` and
    `resume` spend money and stay under the person's rules.

## [0.6.0] - 2026-10-06

### Added

- **cauce as a Claude Code mod.** The plugin also loads `hooks/cauce.tsx`, a
  module of function hooks over the same core. The classic hooks stay, so
  nothing depends on it.
  - **Tools the model calls by name**: `recall`, `note`, `queue`, `tasks` and
    `resume`. They need no PATH and no permission rule (none was asked in
    default mode), and `note` takes only the project's topics.
    - `resume` runs with `--unattended`. It refuses what only a person may
      clear, a refused command or an approval, and says so. No model input
      reaches `--allow`.
  - **The session wakes when work it queued ends.** When a queued task ends
    while the session is idle, cauce starts a turn with the notice, and the
    session acts on it without waiting for the next message. Work that ends
    during a turn is still delivered by the Stop hook. The `wake` setting
    turns it off.
  - **A status entry, a band above the prompt and a `/cauce` pane.**
    - The band lists what waits on you, with a button that continues it with
      the rule cauce suggests: *Allow & resume*, *Approve & resume* or
      *Resume*.
    - The pane cancels running work and confirms or drops notes to review.
- `cauce endings --session <id> [--peek] [--json]`: what ended since a
  session last heard.
- `cauce resume --detach`: run a resume in the background; its ending reaches
  the session that sent the task.
- `cauce resume --unattended`: refuse a resume that only a person can clear.
- `cauce note --json` and `cauce notes topics --json`.

### Fixed

- **An ending could be delivered twice.** The Stop hook and the next prompt
  each read and then marked it, so two at once could both deliver it. Now the
  ending is claimed in one transaction.
- **The board asked to review branches that had already landed.** A merged,
  squash-merged or deleted branch no longer waits on anyone.

## [0.5.0] - 2026-10-06

### Added

- **Project notes: what a project knows, by topic.** Problems and fixes stay
  global. Each project now also keeps notes of what it knows, so a session
  that compacted, or a worker that just started, reads it back instead of
  rebuilding it from the code.
  - **Topics.** `business`, `code`, `decisions`, `conventions` and
    `environment`, plus any a person adds to a project (`cauce notes topic
    add`). Haiku files under the topics that exist. When none fits it
    proposes one, and a person decides.
  - **Links between notes.** `depends_on`, `explains`, `replaces` (retires the
    old note), `contradicts`, `example_of`. Recall follows them one step.
  - **Anchors.** A note is tied to a file, or to a symbol from the livespec
    index (read-only). When a commit that changes it reaches the checkout,
    the note goes to review. A branch not merged yet changes nothing, and a
    squash merge moves the anchor along. `cauce notes ok`, `cauce notes drop`.
  - **Written at three moments.**
    - A passing worker's result block now has `learned`. Haiku files each
      fact, anchored to the files the task changed as its branch has them.
    - Before a session compacts and when it ends (`PreCompact`,
      `SessionEnd`), a detached Haiku reads what was said since the last
      time and keeps the durable facts.
    - A person keeps one with `cauce note "<fact>" --topic <t>`.
    - A failed attempt's facts are never filed, and nothing that looks like
      a secret is kept. Without Haiku, a fact is kept under the topic its
      words point to.
  - **Read back in four places.**
    - Every worker's brief gets the notes that match its task.
    - A prompt that matches notes gets them in context.
    - A session that starts, resumes or compacts gets the project's index of
      notes.
    - `cauce recall "<question>" [--topic] [--path]` answers on demand.
  - **The UI's new Notes tab.** One project's notes by topic, with their
    links and anchors. A note to review can be confirmed or dropped there.
  - **The `notes` setting.** On by default: plugin option, `cauce config
    notes off`, `CAUCE_NOTES`.

## [0.4.10] - 2026-10-06

From running the installed plugin end to end: real `claude -p` workers, a
session queueing with `++` and with `cauce queue add`, a cancel, the UI.

### Fixed

- **An audit was not read as a review.** "Audit calc.py for bugs" matched no
  rule. Haiku read the request as its own task and answered with what it would
  do first ("looking for calc.py"), which is always a search. Its confidence
  swung, so the same audit became `explore` one time and `implement` the next.
  Now:
  - A rule reads an audit or a hunt for bugs (`audit`, `audita`, `busca bugs`,
    `find bugs`, `qué problemas tiene`) as a review, critical when it touches
    auth or money. A verb of change still makes it `implement`.
  - The classifier gets the request as data to classify (`<request>`), told
    not to carry it out.
- **A task queued with `cauce queue add` sat in the queue.** Only `++` started
  the dispatcher. The main session queues through Bash, so its tasks waited for
  a `cauce work` nobody ran. `cauce queue add` now starts the dispatcher too,
  under the same `autowork` setting, and says what happens next.
- **Cancelling a task paused its repository's queue.** The next task waited
  for `cauce lanes --unpause`. A person's cancel is their call on that task,
  not a broken state, so the queue goes on.
- **Haiku named sessions "Unable to determine - incomplete prompts provided".**
  It read titles cut at a few words. It now reads the prompts themselves and
  must always answer with a name.
- **Every repository a session opened got a `.mcp-docs/` folder.** The
  livespec index was built at session start in any git checkout. Now it is
  built only in enrolled projects.
- **The plan warned about refusals that no longer apply.** It listed commands
  refused under `acceptEdits` before auto mode. Now it counts only refusals in
  the mode the next worker runs in.
- **The drawer listed dead ends a task was never shown.** It searched the
  memory as it is now. It now shows what the run and the prompt recorded when
  they showed them.
- **The notice of a finished task left out its answer.** A pass now carries
  the worker's summary and the changed files. A pass that changed nothing says
  so, instead of "its changes are in the checkout".
- **Every Haiku call cauce made was logged as a tool call.** The classifier,
  the dispatcher and the namer answer through `StructuredOutput`, and each
  answer landed in the log that habits are mined from. It is not a step of
  anyone's work, so it is no longer logged.
- **A pass outside a worktree left out where its changes were.** This happens
  in a folder of several repositories. The report now says the changes are in
  your checkout.

## [0.4.9] - 2026-10-06

### Fixed

- **An audit that found problems was read as failed work.** A read-only task
  (review, explore, plan) whose worker found the audited code broken answered
  `fail` / `code_bug`, about the code it read. cauce reads `code_bug` as
  shallow work, so it climbed effort and model. One audit gave the same
  findings seven times, from opus/high to opus/max, for $6.60, and then
  suggested resuming it. Now:
  - A reading worker is told that the answer is the deliverable. Findings,
    broken code included, are a `pass`, with each finding in the summary and
    what shows it in the evidence. `code_bug` is never about the code it
    read.
  - Two cells of a reading task that report the same findings stop the run
    as `converged`. The findings are confirmed and the answer is in the
    worker's account. The task suggests acting on them, not resuming, which
    would repeat them.
  - The notice that tells the main session a task ended now carries what the
    worker found.

## [0.4.8] - 2026-10-06

From a session where one read-only audit went five attempts deep, each refused
different commands, and a person ran it by hand in thirty seconds.

### Changed

- **Workers run in Claude Code's auto mode.** Under `acceptEdits`, every
  command not named by a rule was refused. A worker that needed `node`
  reached for it five ways (`nvm`, `~/.nvm/.../node`, `/opt/homebrew/bin/node`,
  …) and met five refusals, one new `--allow` each. In auto mode a classifier
  lets safe commands through with no rule per command. Haiku is not served by
  auto mode, so Haiku workers run with `bypassPermissions`. This is a
  deliberate choice: Bash in a Haiku worker is unchecked, and deny rules still
  hold, so a reading task's write tools stay removed. `cauce config mode
  <model|default> <mode>` changes either. A CLI that does not take the mode
  runs the attempt again in `acceptEdits` instead of failing.
- **A refused attempt never climbs.** A worker refused its tools, then its
  check went red. That read as shallow work and moved to more effort
  (opus/high → opus/xhigh), which met the same refusals. Any attempt with a
  refusal now blocks, unless the worker found the task itself wrong.

### Fixed

- **Workers were refused the MCP servers cauce handed them.** A capability,
  livespec included, was configured for the worker but its tools were never
  allowed. The tools of every server cauce hands a worker are now allowed
  with it (`mcp__<name>`).

### Added

- **`cauce allow`: rules kept for a repository.** `--allow` grants a rule to
  one run. A new task in the same repository started with none and met the
  same refusals again. `cauce allow 'Bash(npm:*)'` keeps rules for every run
  and resume in the repository, `--preset read|node|python` adds the usual
  sets, and `--rm` takes rules back. They live in the person's cauce home,
  never in the repository. A blocked report also prints the `cauce allow`
  command that keeps its rules.
- **The project's Node goes first on the worker's PATH.** It is read from
  `.nvmrc`, `.node-version` or `engines.node` (for a folder of projects, the
  version that fits them all), found among the versions nvm installed, and
  put first on the PATH of the worker and of its `--verify`. The plan says
  which one. Workers are told to run tools by name, never through a version
  manager or a full path.
- `/orchestration` answers quick read-only lookups itself: a worker's setup
  costs more than a grep.

## [0.4.7] - 2026-10-06

### Fixed

- **Tasks from before 0.4.6 said "you" for text the main session wrote.**
  Back then every task's text was recorded as `user`, and 0.4.6 read `user`
  as the person. Now:
  - text a person typed (`++`, or `cauce run` / `cauce queue add` outside a
    session) is recorded as `person`, so `user` only means a session's own
    prompt, or an older record;
  - on an older task sent from a session, the drawer shows
    "main session (orchestrator, inferred …)", marked as inferred, since
    cauce did not record who wrote it;
  - a session's own prompts, and older tasks run from a terminal, still say
    "you".

### Added

- **The main session hears when the work it sent ends.** A queued task, or a
  run in the background, could end while the main session waited on nothing,
  so it never told the person, continued or relaunched. Now each ending is
  reported to the session that sent it, once:
  - when the session is about to end its turn, the Stop hook blocks it once
    with what ended, why, what changed so far and the command that goes on;
  - when the session is idle, it hears with the person's next message;
  - a `cauce run` or `cauce resume` whose report the caller read already
    counts as reported.

  Only endings of the last 24 hours are reported, so old work does not come
  back.
- **A task that outgrew its turns can be continued.** A `replan` because a
  task did not fit the raised turn budget is not a wrong task. It now resumes
  with `cauce resume <id> --max-turns 120` (or any `--max-turns`), and in a
  worktree its work is kept on its branch instead of dropped.

## [0.4.6] - 2026-10-06

### Fixed

- **The UI listed sessions from every folder, not just enrolled projects.**
  To find a session's `.cauce/`, cauce climbed every parent folder, and
  outside a git checkout it went all the way to `/`. So one `.cauce/` high up
  enrolled every session below it: in a folder that holds several projects,
  in the home directory, or at the top of a home that is itself a git
  checkout (dotfiles). Now a session belongs to the `.cauce/` in its own
  directory, or in a parent up to the top of its checkout, and never above it.
  Outside a checkout only its own directory counts. The home directory and `/`
  are never a project, and `cauce init` refuses them. A stray `.cauce/` left
  in a high folder no longer lists anything below it, with no cleanup needed.
- **Work in nested repositories read as "changed nothing".** A task run in a
  folder that holds several git repositories reported `changed (from git):
  nothing` while its worker had edited two of them. The folder is not a
  checkout itself, and a checkout's own status never looks inside a nested
  one. Each attempt now also reads every git checkout up to two levels below
  its work directory (dependency folders aside), with paths prefixed by
  where each one lives, including what a worker committed in them.
- **Rules read back from older refusals missed programs.** A refusal cut at
  120 characters lost its last command even when only that command's
  arguments were cut, so `~/.local/bin/x -n "…" src/...` gave no rule.
  A refusal cut inside a quote lost everything after the quote opened. Now
  the program is kept whenever it is whole, and a cut quote is closed before
  the line is read.
- **The task's text was always labeled "user".** When the main session sends
  work through its own Bash (`cauce run` or `cauce queue add` with
  `CAUCE_SESSION_ID` set), or delegates to a subagent, the text is the main
  session's, not something a person typed. It is now recorded as
  `orchestrator`. The task drawer labels each message by who wrote it: you,
  the main session (orchestrator), the main session's answer, or cauce's
  report. Older tasks keep `user`.
- The task drawer no longer shows an empty "evidence" block for an attempt
  whose evidence was blank.

### Added

- **The plan says when a task needs what a worker does not have.**
  - **A browser:** words like "browser", "navigate" or "screenshot", with no
    browser capability registered.
  - **Running servers:** "dev servers", `npm start`, `ng serve`,
    `localhost:<port>`. A server a worker starts stops when the worker
    finishes.

  The note appears before any money is spent, in the report and in
  `--dry-run`.
- `cauce capabilities --example` includes a browser (Playwright MCP) for
  `test` tasks, and for `ui` tasks after a failure.
- Workers are told that a command that never returns blocks the call it runs
  in. They should start it in the background and stop it before they finish.
  Without a browser they answer `environment`, never a description of a page
  they did not open.

## [0.4.5] - 2026-10-06

### Added

- **Which model actually served each attempt.** A worker is started with an
  alias (`--model sonnet`), and the installed Claude Code decides which model
  that is. An older Claude Code can still map `sonnet` to an earlier Sonnet
  while a person's own sessions already run the newer one. Each attempt now
  records the model id the CLI reports in its `modelUsage` (the one that did
  most of the work). The report, `cauce show` and the task drawer show it.
- **Pin an alias to a model id:** `cauce config model sonnet claude-sonnet-5-5`
  (or `CAUCE_MODEL_SONNET`). `cauce config model sonnet default` unpins it, and
  `cauce config model` lists every alias with its pin and the model that last
  served its workers. Unpinned aliases go through as before, so cauce keeps
  following Claude Code and any provider's model names. The classifier's
  Haiku honours the pin too.
- **The plan warns when workers run an older model than your sessions.** If
  the newest model of a family in the person's sessions is a newer version
  than the one that last served the workers, the plan (and `--dry-run`) says
  so, with the command that pins it.

### Fixed

- `store.home(env)` took the home directory from the running process instead
  of the environment it was given. With that, a test leaked livespec index
  locks into the real `~/.local/share/cauce`.

## [0.4.4] - 2026-10-06

From a review of real sessions where most blocked tasks were finished by
hand.

### Fixed

- **The suggested `--allow` rules could not be used.** A refusal was suggested
  as the whole call, chained and cut at 120 characters, e.g.
  `Bash(search -n "x" src; echo "exit $?"; g...)`. Allowing that allowed
  nothing. Refusals now become one rule per program, read from the full call
  (`src/cauce/allow.py`):
  - a shell line is split into the commands it chains, and each one gets a
    prefix rule: `Bash(npx tsc:*)`, `Bash(npm run build:*)`,
    `Bash(git status:*)`. `bash -c` is read for its script;
  - an absolute file path gets two slashes (`Read(//abs/path)`). With one,
    Claude Code reads it relative to the settings file;
  - a URL becomes its domain.

  The report, the `blocked` reason, `cauce show`, the board and the resume
  command use these rules. Tasks recorded before get them read back from what
  was refused.
- **A task started with `--no-isolate` resumed in a worktree.** There it could
  not see the uncommitted work it was continuing. The choice is now kept with
  the task's options; `cauce resume --no-isolate` also sets it.
- **One click on Cancel in the task drawer stopped a run.** Cancel now takes
  a second click within four seconds.

### Changed

- **A worker that runs out of turns or money is checked by cauce.** Before,
  such a worker never reached `--verify`, and a large change ended without
  its tests ever running. Now cauce runs `--verify` itself whenever the task
  changed something: a green check passes the attempt, and a red one gives
  the next attempt the failing output, at the end of its brief. Every failing
  attempt's output now reaches the next brief this way.
- **Turns are raised while the work keeps moving.** A run that hit its turn
  ceiling twice used to be sent back to be split, even when the raised
  attempt had changed dozens of files. Now a raised attempt that changed files
  is raised again (30 → 60 → 120 → 200). A raised attempt that changed
  nothing, or the 200 cap, still means split it.
- **The plan names the rules workers in this repository lacked before**, with
  counts, leaving out those already granted. `cauce run --dry-run` shows them,
  so they can be passed with `--allow` from the start.
- **Workers are told how refusals work.** Nobody can approve a command while
  they run: read and search with the Read, Grep and Glob tools, and run one
  shell command per call, since one refused part refuses the whole chain.

## [0.4.3] - 2026-10-06

### Added

- **Every climb says where it went and why.** A move after a failed attempt
  used to keep only its name and a short reason ("code_bug: same model, more
  thorough"). Its destination could only be read off the next attempt. Each
  decision (`escalate.decide`) now carries the full account:
  - the dial it turned: effort, model, turns, retry, or stop;
  - from which cell to which, and the turns before and after;
  - the evidence chain: what the attempt ended with and what that failure
    means, the rule it was read against (shallow work climbs effort; a wrong
    approach or two cells agreeing changes model; a refusal is a setting),
    and the cell it goes to with that effort's purpose;
  - the ladder cells it skipped, and why;
  - the budget left.

  The `moved` event keeps all of it (with its attempt's `seq`).
- **Where it shows:**
  - The run's report and `cauce show` print the account under each attempt.
  - The task drawer shows each move as from → to, with the numbered evidence
    and the skipped cells.
  - The task drawer's plan draws the ladder with each attempt on the cell it
    ran at: red where it failed, green where it passed.
- **Routing** gains *How each ladder was climbed*: per kind, every move with
  the cell it left, the failure behind it, where the next attempt ran, how
  many times, and how often that next attempt passed. This is the evidence
  for raising a start or dropping a rung. It is built from the attempts, so
  tasks from before 0.4.3 count too.
- A task from before 0.4.3 shows its moves from what its attempts recorded,
  marked as such.

## [0.4.2] - 2026-10-06

### Added

- **Every task that stops says why, who made the call, and what continues it.**
  A *Needs you* card used to say the same sentence for every `blocked` task,
  and a cancelled task did not say whether a person cancelled it. Each place
  that ends a task without a pass now records one account on its `finished`
  event (`src/cauce/stops.py`): the cause (`permission`, `budget`,
  `environment`, `approval`, `spec`, `turns`, `ladder`, `exhausted`,
  `attempts`, `cancelled`, `signal`, `died`, `missing_dir`, `crashed`), who
  made the call, the reason in full, the refused rules, the last worker's own
  account, and the command that continues it, such as
  `cauce resume 12 --allow 'Bash(npm run build)'`, `--allow-approval` or a
  doubled `--budget`. Those places are the run's own loop, `cauce cancel`,
  `cauce queue rm`, the UI's cancel, Ctrl-C, the dispatcher and the sweep.
  A cancel records where it came from (the UI, `cauce cancel`,
  `cauce queue rm`, Ctrl-C). A SIGTERM nobody asked cauce for is told apart
  from a person's cancel, and so is an error in cauce itself.
- The board's cards and the task drawer show that account, with the command
  ready to copy. The drawer also shows what each attempt was refused and the
  evidence of each failure. A task that stopped before 0.4.2 gets an account
  read back from its attempts and events, marked as read back.
- `cauce show <id>` prints each attempt's refused rules and the account. When
  a session resumes or is compacted, it is told which of its tasks stopped for
  a person, why, and the command, so it can tell them instead of guessing.
- The session drawer shows what each open item of Claude Code's own task list
  waits on (`blockedBy`).

### Fixed

- `cauce resume` lost what earlier attempts were refused: the resumed brief
  no longer said `refused: …`. It is read back from the attempts' events.

## [0.4.1] - 2026-10-05

### Fixed

- **The board kept showing every prompt in Done after the update that
  stopped it (0.1.2).** A `cauce ui` server keeps the code it started with,
  and a plugin update never restarted it: a board started in a session before
  an update went on serving that version's board. Now the server checks
  every 30 seconds what Claude Code has installed. When a newer cauce is
  there, it runs that version's UI in its own place, on the same port. The
  open page reads the new `GET /api/version` and reloads when the version
  changes. A UI from before 0.4.1 cannot do this itself: stop it once.
- **`cauce ui` on a taken port** failed with a Python traceback. It now says
  who holds the port. If it is this same version, it prints the board's
  address and opens it with `--open`. If it is an older cauce UI, it explains
  that this server shows the old board until it is stopped. If it is another
  program, it suggests another port.

## [0.4.0] - 2026-10-05

From an audit of a session where five tasks ended `blocked` and their reports
said "I created the routes" while the repository had none of them.

### Fixed

- **Work a worker did was thrown away when its task stopped.** A task that
  ended `blocked` or `needs_approval` had its worktree and branch deleted, so a
  worker's true account of what it wrote read as a lie. That work is now kept
  on `cauce/task-<id>`, committed as unverified, and the report says so.
  `failed`, `replan` and `cancelled` still leave nothing.
- **A refused command was retried, then reported as "the environment failed
  twice".** A one-shot worker is refused whatever its settings do not allow
  (`npm run build`, `node`). cauce now reads the CLI's own
  `permission_denials`. A refusal is the new `permission` failure, and it
  blocks at once: retrying it, or sending a stronger model into it, gets the
  same refusal. The report names each refused call as the rule to allow.
- **A fresh worktree could not build.** It has none of the checkout's ignored
  folders. The checkout's ignored `node_modules`, `.venv` and `venv` are now
  linked in, and the worker may read them. The person's
  `.claude/settings.local.json` is copied in, so a worker is allowed (and
  refused) what the person set. Neither is ever committed.
- **A pre-commit hook could lose a task's work.** cauce's own snapshot commit
  on the task branch runs with `--no-verify`. A hook that needs approval, or
  an install the worktree lacks, no longer stops the work being kept. The
  person's hooks run when they commit or merge.
- **A request to create files could get a read-only worker.** "Crear 5
  módulos…" read as `explore` gets a worker with no write tool, which can only
  describe changes it did not make. A request that opens a clause with a verb
  of change (create, add, register, fix, update…, in English and Spanish) is
  never read as `chat`, `explore` or a review, unless a `#tag` or `--kind`
  says so. A read-only worker asked for a change answers `spec_bug`, so the
  run ends `replan`.
- **"Fix it, then resume" named no command**, and the orchestrating session
  tried `cauce continue`, which never existed.

### Added

- `cauce resume <id>` runs a task that stopped (`blocked`, `needs_approval`,
  `failed`, `cancelled`, `interrupted`) again under its own id. It continues
  on its kept branch from the cell it stopped at, with its earlier attempts in
  the brief. It reuses the task's verify command and grants; `--allow`,
  `--verify`, `--budget`, `--start` and `--allow-approval` add to them or
  replace them.
- `--allow "<rule>"` on `run`, `route`, `queue add` and `resume` (repeatable),
  e.g. `--allow "Bash(npm run build)"`. It is passed to workers as
  `--allowedTools` and kept with the task.
- Every report of a writing task prints `changed (from git): …` next to the
  worker's summary. The summary is labelled as the worker's own account, and
  a kept branch says whether it is ready to merge or unverified.
- `--verify` can settle an attempt whose worker was refused the command that
  would check its work, as long as the task has written something and the
  worker did not call it a failure. A green check is a pass; a red one stays
  blocked, with the check's output as evidence. The worker's claim alone is
  never enough.
- `/orchestration` tells the session to grant commands up front with
  `--allow`, to say a file was created only when `changed (from git)` lists
  it, to resume a blocked task instead of redoing it, and to use only the
  commands `cauce --help` lists.

## [0.3.2] - 2026-10-05

### Fixed

- A task failed with livespec's "database is locked". Several refreshes of one
  livespec index could run at once — the session-start refresh, and since
  0.2.0 every parallel task planning in the same repository — and livespec
  lets only one through. Refreshes of one index file are now serialized under
  a lock in cauce's home: a refresh that finds another running waits for it and
  then indexes only if the index is still behind; the session-start refresh
  runs as `cauce index-livespec` under the same lock and is not started while
  one runs. Reproduced with three real refreshes on a fresh repository: one
  failed before, none after.

## [0.3.1] - 2026-10-05

### Fixed

- Habit candidates listed false positives: `find → ls`, `mkdir → cd`, `cat →
  cd → cd` repeat in every session, but no hook could take them over and
  installing one would save nothing. A candidate now has to start with an
  edit or a write (what a hook is triggered by) and end in a command that does
  something — not a program that only looks, moves around or prints.

## [0.3.0] - 2026-10-05

### Added

- **Enrolled projects.** A project is cauce's when it has a `.cauce/` folder,
  at the top of its checkout or where the session runs. Only enrolled projects
  and their sessions show in the UI and in `cauce projects` / `cauce sessions`
  (`--all` lists the rest). cauce enrolls a project the first time work is
  queued or run there; `cauce init` does it by hand. The folder ignores itself
  in git.
- **Session names.** Haiku names each session of an enrolled project in
  `.cauce/sessions.json`, after its first prompt and every ten after, started
  detached from the Stop hook (`names`, on by default, a plugin option). A
  name a person edits there is theirs and is never replaced. The UIs show the
  name, with the last prompt under it.

### Changed

- The board no longer carries a line explaining where work is asked for.

## [0.2.0] - 2026-10-05

### Changed — who decides what waits

- **Haiku decides whether a queued task runs in parallel or waits its turn**
  (`parallel`, on by default). The first task of an idle repository starts;
  one queued behind running work starts beside it only when Haiku, reading it
  against everything running and queued ahead, finds it independent. The
  decision is made once, kept on the task with its reason, and fails closed:
  no answer means waiting. Up to three tasks run at once per repository.
  `parallel off` keeps one serial lane per repository.
- **The dispatcher runs each task as its own process** (`cauce run-queued`),
  so a task's pid, its cancel and the sweep are its own, and it watches several
  at once. One dispatcher per repository, held by a lock file; a second
  `cauce work` for the same repository says so and exits.
- **`++` starts the dispatcher** (`autowork`, on by default): queued work runs
  without anyone typing `cauce work`. Unpausing a lane in the UI resumes it.
- **Work typed in a session is that session's.** SessionStart exports
  `CAUCE_SESSION_ID` to the session's commands, so `cauce run` and `cauce queue
  add` record it; workers and the dispatcher never inherit it.
- Both new settings are plugin options, like `livespec`.

### Changed — the UI

- **One project at a time, always.** A project picker at the top; the board and
  the sessions are that project's, and `/api/board` and `/api/sessions` refuse
  a request without one. The board narrows further to one session.
- **Sessions** screen: every session cauce saw in the project with its resume
  command; opening one shows its own task list as Claude Code keeps it (read
  from `<config>/tasks/<session>/`, or the older `todos/` file — never
  written; absent and unreadable told apart), the work it gave cauce, and its
  recent turns.
- **No task is typed into the page.** The queue form and `POST /api/queue` are
  gone: work is asked for in a session, where it has the session's context and
  record. Queued cards show Haiku's call and its reason.

## [0.1.2] - 2026-10-05

### Fixed

- The board showed every prompt typed in a session as a card, so Done filled
  with turns and pushed real tasks out of its 60 slots. The board now holds
  only work cauce runs or queued (`source` cauce or queue), filtered in SQL;
  the sessions answering a prompt right now are listed apart as `answering`
  (a count under Running in the UI, and `counts.answering`). Prompts are still
  recorded: they couple follow-ups and results to a turn, list unfinished
  work after a resume or compaction, and feed the Sessions view.

## [0.1.1] - 2026-10-05

### Added — the core

- **Model × effort routing.** Fifteen kinds of work, each with a ladder of
  cells; all five effort levels used, each for its own purpose; Haiku without
  an effort; Fable only on a `#fable` tag or `--allow-approval`.
- **Classification:** rules that are right when they fire, then Haiku with a
  JSON schema and no tools; below 0.6 confidence, or on any failure, the
  default kind.
- **Escalation on evidence:** shallow work climbs effort, a wrong approach or a
  repeated answer changes model, a timeout retries once, a turn ceiling is
  raised once, a wrong task replans. A worker's pass needs verbatim evidence;
  `--verify` has the last word.
- **One-shot workers** via `claude -p`: per-attempt model, effort, turn and
  dollar ceilings; read-only kinds get no edit tools; exactly the selected MCP
  servers; launched in one directory and working in another; no session kept.
- **Isolation:** one git worktree per writing task; a passed task leaves one
  branch, a failed one leaves nothing.
- **Capabilities** from a user-level registry: always, per kind, or after a
  failure; a hint per capability goes into the worker's system prompt.
- **Memory** in one SQLite file: tasks, the messages coupled to them, every
  attempt (the reroute trace), and problems with the fixes tried — searchable
  across repositories, this one first. History raises where a kind starts.
- **Hooks:** a prompt is a task, a follow-up joins its turn, a bare "continue"
  folds into the turn it pushes, a matching dead end reaches the model before
  it starts. Hooks fail open and report what they swallowed.
- **`/orchestration`**, the one entry point; the `cauce` CLI for everything else.

### Added — habits

- Every tool call, in sessions and workers, is logged as a signature plus an
  argument hash — never arguments, contents, output or prompts — by a hook
  fast path that appends one line to a file (about 50 ms with the launcher).
- `cauce habits`: repeated sequences past boolean gates, ranked by score.
- Recipes: the steps before passing worker attempts of a kind go into the
  next worker's brief.
- `cauce habits install|uninstall|status`: a habit becomes a `PostToolUse`
  hook only by that command; three failures in a row turn it off.
- The classifier on a local Laya model was not taken: rules and Haiku already
  route, and Laya needs a 1–3 GB install.

### Added — memory that remembers being wrong, and real spend

- Fixes carry `believed_from` and `invalidated_on`; a solved problem that
  turns up again is `recurring` and its fix disproved; `cauce memory
  invalidate`. Disproved fixes are dead ends, with what worked instead.
- A fix that worked is anchored to the commit on the task's branch.
- Token usage per model is read from the session transcript at `Stop`
  (incrementally, one record per message, a runaway line skipped);
  `cauce spend` joins it with worker spend by cell. Plans state what
  finished tasks of the same kind cost on average.

### Added — live state, the queue and its lanes

- An event trail per run (`cauce events --follow`), the cell in flight, and
  `cauce cancel`.
- `++ <task>` in a session, or `cauce queue add`, queues work at zero tokens;
  `cauce work` drains it in workers. One serial lane per repository, paused by
  a task that does not pass; `cauce lanes` shows and reopens them.
- A sweep marks runs whose process died, and session prompts abandoned for 12
  hours, as interrupted.
- Subagent delegations are child tasks; a turn is not done while one runs,
  background ones included (closed by their task notification).
- Pinned cells are recorded and never teach the router.

### Added — the UI

- `cauce ui`: a local page with a board (needs you, running, queued per lane,
  done by day), a task drawer with the attempt timeline, and spend, routing,
  memory and habits screens. Live through server-sent events from the event
  trail, with a polling fallback.
- Queue, cancel, reopen a lane and start `cauce work` from the page. Loopback
  only, `Host` allowlist, token header on every mutation, strict CSP, 1 MiB
  body cap; housekeeping in a server thread, never in a GET.
- `cauce board --json` for status lines.
- `/cauce:ui` opens the page from a session. `cauce board --full [--repo DIR]…`,
  `cauce show <id> --json` and `cauce queue add … --json` give another program
  the board scoped to some repositories, a task, and what it queued.
- The board carries each task's way through the matrix (`flow`: kind, ladder,
  start, every attempt with its cell, outcome and move) and, for a running task,
  the worker out now (`worker`: the `claude -p` of this attempt, its cell, turn
  and dollar limits, MCP servers, start time, and whether its process lives).
  `counts.workers` says how many are out.
- `cauce projects --json`: every repository cauce has worked in, from the
  directory it was last used from. `cauce sessions --json [--repo DIR]`: the
  Claude Code sessions it saw, with their prompts and the command that resumes
  each. `cauce memory list --json [--query Q] [--repo DIR]`: problems and every
  fix tried, everything or some repositories'. Board items carry the task's
  text (`body`, up to 800 characters) and its `session_id`.

### Fixed

- Two processes opening an old database at once no longer fail on a column
  the other one just added.
- Opening the database is serialized across processes (a lock file beside it)
  and a "database is locked" during setup is retried: switching to WAL and
  altering a table could be refused at once instead of waiting, when a hook,
  the dispatcher and the UI opened the file in the same instant.
- `cauce` not found (exit 127) in a session of an older Claude Code, or one
  opened before the plugin was installed: `SessionStart` puts the plugin's
  `bin/` on the session's PATH through `CLAUDE_ENV_FILE`, or names the full
  path when it cannot, and the commands fall back to the full path too. The
  launcher follows symlinks and says so when no Python is found.
- A `python3` older than 3.11 hands over to a `python3.11`–`3.14` beside it,
  with no extra process when `python3` is new enough; with none, or with an old
  `CAUCE_PYTHON`, one line says so instead of an import traceback. The
  hand-over marker is never inherited by a worker's hooks.
- `cauce link`: `cauce` in a terminal. It writes a shim to `~/.local/bin` that
  runs the newest installed cauce, so a plugin update does not break it, and
  never replaces a `cauce` it did not write.

### Added — livespec is built in

- **Adapters**: neighbours the core adopts rather than lists. The livespec
  adapter reads the code index read-only with `sqlite3` and takes part in
  routing (critical specs and widely called symbols raise the start; a review
  of critical code is reviewed as critical), briefing (a code map at the top of
  every worker's prompt), the work (livespec's server with a per-kind hint and
  the `workspace`) and the report (specs touched, callers outside the change).
- An unindexed or stale repository is indexed through livespec's own CLI before
  a run, and in the background at session start.
- livespec needs no separate install: an installed plugin, a `livespec` on
  `PATH`, or the pinned `livespec@0.32.0` through `uvx`.
- **The `livespec` switch**, on by default: plugin option, `cauce config
  livespec off`, `CAUCE_LIVESPEC`, `--no-livespec`. `cauce neighbours` shows
  what cauce sees.
