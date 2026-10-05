# Changelog

## [Unreleased]

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
