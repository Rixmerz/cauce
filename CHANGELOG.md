# Changelog

## [Unreleased]

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
