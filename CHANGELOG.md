# Changelog

## [Unreleased]

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
