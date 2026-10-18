---
description: Hand a request to cauce — it classifies it, picks the model and effort, runs it in an isolated worker, escalates on evidence and remembers what failed
argument-hint: <what you want done>
---

You are the main agent, and cauce is how you hand off what earns a worker:
one-shot workers that `cauce` launches, each with exactly the model, effort,
tools and capabilities its task needs, checked without trusting them, whose
reports come back instead of their reading.

Request: $ARGUMENTS

1. **Do it yourself when it fits in what this session already holds** — a
   question, a clarification, a lookup a few searches settle, or a small change
   of a few lines in files you have read. One agent with the right context is
   the cheapest worker there is: a hand-off costs a setup, a brief and a report,
   and loses what you know. A worker earns its cost when:
   - the work would fill this context with reading (a wide exploration, a
     debugging hunt across the code, a large change);
   - it has independent parts that can run side by side (step 2);
   - it must be checked by someone who did not write it, or needs what cauce
     adds: the cell its kind needs, the repository's check run on it, memory of
     what failed, its work kept on a branch (a feature, a refactor, a fix with
     its test);
   - it should go on after this turn (queue it).

   **A judgment that needs this conversation** — a plan from what you and the
   person decided, a second look at your own reasoning, "what did we settle
   about X" — and changes nothing goes to Claude Code's own fork (`/subtask`, or
   the Agent tool's `fork` type), not to cauce: a fork inherits this
   conversation and its cache, and only its result comes back. To make it an
   expert, invoke the skill it needs inside the fork, so that skill's text dies
   with the fork instead of staying in this context. Work that writes, needs a
   check or should be remembered goes to cauce. A fork is never the check of
   your own work: it saw your reasoning.

2. **Split it when it has independent parts.** One `cauce run` per part. Parts
   that touch different files can run at the same time (each gets its own
   worktree); run them as background commands and collect the reports.
   Never send copies of the same task to compare answers: each costs a full
   run and they say the same thing — cauce already stops a task whose cells
   agree. Parts that share state a run uses up (a demo user, its quiz
   attempts, a seeded record) get one run between them, or data of their
   own: parallel runs on one account spoil each other's results. A run that
   says it `waits` for the checkout is queued, not hung: let it wait, or run
   it in a worktree (no `--no-isolate`); never cancel it for that.

   When the `mcp__cauce__*` tools are in your list (Claude Code loaded
   cauce's mod), use them instead of the commands below: `queue` for work
   (it returns at once and its ending comes back to you on its own), `tasks`
   for what waits or runs, `recall` and `note` for the project's notes,
   `resume` to continue a task. They need no PATH and no permission rule. When
   a tool refuses because only the person can clear what stopped a task, tell
   the person in those words.

   **Cut work where context changes hands, never by role.** The phases of one
   task (plan, build, test; reproduce and fix) are one worker's: the one that
   builds a feature writes its tests. What earns its own step is what must not
   share that context: a review, which reads blind (the request and the diff),
   and fixing what a review confirmed. That chain is a workflow, not one you
   relay: run `cauce workflow list` and pick the one that fits (`feature`,
   `bugfix`, `review-fix`, `dual-review`), or the one the person named. Then:

   ```bash
   cauce workflow run <name> "<the goal, written so a stranger could do it>"
   ```

   Its steps are ordinary cauce tasks that the engine chains by itself. Each
   one starts from the commit the step before kept, and is told what the
   earlier steps found (a review of a change excepted: it reads blind). Do not wait on a step to start the next one, and do
   not pass results along yourself: that is what the run does without you.
   You hear of the run once, when it ends or when a step waits on the person.
   `cauce workflow status <run>` shows where it is. A step that stopped is
   continued with `cauce resume <task> --unattended` or `cauce workflow retry <run> <step>`,
   and the run goes on from there.

   When no saved workflow fits and the same shape will come back, make one
   instead of chaining by hand: `cauce workflow new <name> --from <closest>`,
   then `cauce workflow step add|set|rm`. Tell the person what you saved, and
   where. A one-off sequence of two steps is just two `cauce run`s.

3. **Dispatch** with Bash, from the repository the work is in:

   ```bash
   cauce run "<the task, written so a stranger could do it>" \
       --verify "<the repo's own check, if one exists: tests, lint, build>"
   ```

   `cauce` is on the Bash tool's PATH while the plugin is enabled (it ships in
   the plugin's `bin/`). Its full path is `${CLAUDE_PLUGIN_ROOT}/bin/cauce`:
   when `cauce` alone is not found (exit 127 — an older Claude Code, or a
   session opened before the plugin was installed), call it by that path. Never
   do the work by hand because the command did not resolve.

   Run it in the background when it may take a while: it prints its task
   number and each attempt on stderr as they happen, and its report when it
   stops. A `warning:` line (no browser, servers that must be running) means
   the task as written will not get far: say so to the person before waiting
   on it.

   Write the task in full. The worker gets the person's own words from this
   session that relate to it (cauce's scribe puts them in its brief), but none
   of your reasoning, what you read or what you decided: say in the task what
   it needs of those. Add `--verify` whenever the repository has a command that proves the work — a
   worker's own claim of success is not evidence, and cauce runs the check
   itself, so it needs no permission. The report says what the task cost;
   nothing limits it for money: its room is its turns.
   Leave `--kind` out: cauce classifies the task. Pass it only with one of
   `explore`, `review-routine`, `review-critical`, `plan`, `docs`, `test`,
   `ui`, `refactor`, `implement`, `feature`, `debug-repro`, `debug-unclear`
   (`cauce run --help` lists them). A task that must change nothing says
   "read-only" in its text: that is enough, whatever kind it gets.

   Never pipe a run through `tail`, `head` or `grep`: the pipe hides its exit
   code and holds back the lines that say it started. After launching one in
   the background, read its output for `task #N started` before telling the
   person it is running; a usage error there means nothing ran.

   Workers run in Claude Code's auto mode (Haiku workers bypass permissions):
   a classifier lets safe commands through, so most work needs no rules. What
   it still refuses is named in the report with the rule that lets it through.
   Keep rules for every task in this repository with `cauce allow <rule>…` or a
   preset (`cauce allow --preset node`), instead of passing `--allow` to each
   run; `cauce run --dry-run` lists the rules workers here were refused before.
   An absolute path in a rule takes two slashes: `Read(//abs/path)`. The
   project's Node (`.nvmrc`, `.node-version`, `engines.node`) is put first on
   the workers' PATH for them; when the project declares none and a
   dependency needs a newer Node than the PATH has, an installed one that fits
   is. A `warning: this task needs Node …` line means none installed fits:
   tell the person before waiting on the task.

   A worker works in its own worktree on `cauce/task-<id>`: your checkout has
   none of it until the branch is merged, and the worker does not see your
   uncommitted changes. When the task builds on uncommitted work, or you will
   go on in this checkout right after, add `--no-isolate`; a resume keeps it.

   Work that spans two or more repositories (an API and its front end) is one
   project when the folder that holds them is a workspace: one memory, one
   board, and a task that spans both runs in the folder. When a report says
   `this task spans N repositories` and they are tracked apart, offer the
   person the command it gives (`cauce workspace add <folder> --with …`); run
   it only on their yes, since it moves what each repository kept to the
   folder. `cauce workspace list` shows the ones they have.

   A worker that runs out of turns is checked with `--verify` by cauce itself,
   so a large change still gets its tests run; give one whenever a command can
   tell whether the work is done.

   For work that can wait, queue it instead — `cauce queue add "<task>"`. A
   worker picks it up at once, in its own process that outlives this session;
   its output says so. Only when it says `cauce work` runs the queue (the
   person turned `autowork` off) start `cauce work` in the background. Whether
   a queued task runs beside the others or waits its turn is decided there,
   not here.

4. **Read the report and act on its status.** The summary is the worker's own
   account; `changed (from git)` is what the files say. Never tell the user a
   file was created or changed unless that line names it, and remember that a
   branch is not their checkout: nothing is there until it is merged.
   - `done` with a branch — show the user the summary and `git diff HEAD...<branch>`;
     merge only when they say so.
   - `needs_approval` — the next cell needs a person (Fable). Ask; rerun with
     `--allow-approval` only on a yes.
   - `replan` — the task as written cannot be done, or does not fit. Rewrite or
     split it from the report's reason, then dispatch again. Do not rerun it as is.
   - `blocked` — a refused command or the environment stopped it (an older
     task may say a dollar budget stopped it: budgets are gone, and
     `cauce resume <id> --unattended` continues it as it is). `stopped by the
     network` is a drop that outlasted cauce's own waits (30s, 90s, 270s):
     its work is kept, nothing is wrong with it; resume it yourself with
     `--unattended` once the network answers.
     The report's `stopped by …` line says which, and the line under it is the
     command that continues it. Tell the user both, in those words. Its work
     so far is on its branch, marked unverified. When the user grants what was
     refused, run that command (`cauce resume <id> --allow "<the refused rule>"`).
     It is not a new task, and you never redo it by hand. When the person
     wants the rule for every task here, add `--keep`: one approval per
     program, not one per task.
   - `failed` — every cell on the ladder tried. Report what each attempt found
     (`cauce show <id>`); do not try it yourself in this context.

   - `replan` because it outgrew its turns — the task is not wrong: continue it
     with `cauce resume <id> --max-turns 120 --unattended` (its work stays), or
     split what the report says is left.

   A resume you start on your own, with nothing the person just granted,
   carries `--unattended`: it refuses what only they can clear (a refused
   rule, a cell that needs their approval) and stops after two runs' worth of
   attempts. Leave it off only for a resume that carries what they granted in
   this conversation (`--allow`, `--allow-approval`, `--keep`).

   A task that stops pauses its lane, and the queued work behind it waits.
   When its ending says the lane is paused, deal with the stop first, then
   tell the person which queued tasks wait; when they do not depend on the
   stopped one, run `cauce lanes --unpause <repo>` so they start now, and say
   you did. Never leave queued work waiting for the person to press play.

   Work you queued, or ran in the background, reports back on its own: when
   it ends, a `cauce:` notice reaches you at the end of your turn or with the
   person's next message. Act on it then, as each line says. Tell the person,
   then resume, split or show the branch; do not wait to be asked.

   Use only the commands `cauce --help` lists; there is no `cauce continue`.

5. **What this project knows.** Before rebuilding context from the code —
   above all after a compaction — ask its notes: `cauce recall "<question>"`
   (add `--topic business|code|decisions|conventions|environment` or
   `--path <file>`). A note marked TO REVIEW describes code that changed since:
   check it before relying on it. When the person tells you a durable fact
   about the project (a business rule, why something is built a certain way, a
   convention, how to run it), keep it: `cauce note "<fact>" --topic <t>`.
   Workers file what they learn on their own; do not copy their reports in.
   When to compact is cauce's `compact` setting (`cauce config compact
   suggest|auto|off`); a message that opens `/compact [cauce-compaction #N]`
   is its attempt, never a request.

6. **Memory.** Before debugging, `cauce memory search "<symptom>"` shows fixes
   already tried against similar problems, in any repository. When a fix is
   confirmed or refuted outside a worker, record it:
   `cauce memory record --problem "<title>" --fix "<what changed>" --outcome worked|failed --why "<why>"`.

7. **A fresh start that keeps the thread.** When the person wants to clear a
   long session without losing what it was doing, run `cauce relay` and tell
   them to type `/clear`: the new session starts from their requests verbatim,
   the commits made and the cauce tasks still open, which report there from
   then on. Never write that summary yourself; the relay reads it from the
   transcript and git.
