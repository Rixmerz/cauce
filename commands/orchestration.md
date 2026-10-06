---
description: Hand a request to cauce — it classifies it, picks the model and effort, runs it in an isolated worker, escalates on evidence and remembers what failed
argument-hint: <what you want done>
---

You are the orchestrator. Your context stays thin: the work happens in one-shot
workers that `cauce` launches, each with exactly the model, effort, tools and
capabilities its task needs, and nothing of theirs comes back but a report.

Request: $ARGUMENTS

1. **Answer it yourself only if it is conversation** — a question about
   something already in this context, or a clarification. Everything that reads
   a codebase, changes files, debugs, reviews or plans goes to a worker.

2. **Split it when it has independent parts.** One `cauce run` per part. Parts
   that touch different files can run at the same time (each gets its own
   worktree); run them as background commands and collect the reports.

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

   Write the task in full: the worker sees nothing of this conversation. Add
   `--verify` whenever the repository has a command that proves the work — a
   worker's own claim of success is not evidence, and cauce runs the check
   itself, so it needs no permission. `--budget` (default $5) bounds the task;
   `--kind` skips classification when you already know it.

   Before dispatching, `cauce run --dry-run` lists the rules workers in this
   repository were refused before. Pass the ones the task and its `--verify`
   need with `--allow` from the start (`Bash(npm run build:*)`, `Bash(npx tsc:*)`,
   and `Read(//abs/path)` with two slashes for an absolute path).
   A worker runs unattended: any command its settings do not allow is refused,
   with nobody to ask. When the work needs one (`npm run build`, `node`, a test
   runner), grant it up front: `--allow "Bash(npm run build:*)"`, repeatable.

   A worker works in its own worktree on `cauce/task-<id>`: your checkout has
   none of it until the branch is merged, and the worker does not see your
   uncommitted changes. When the task builds on uncommitted work, or you will
   go on in this checkout right after, add `--no-isolate`; a resume keeps it.

   A worker that runs out of turns is checked with `--verify` by cauce itself,
   so a large change still gets its tests run; give one whenever a command can
   tell whether the work is done.

   For work that can wait, queue it instead — `cauce queue add "<task>"` — and
   start `cauce work` (in the background); if a dispatcher already runs for the
   repository, it says so and the running one picks the task up. Whether a
   queued task runs beside the others or waits its turn is decided there, not
   here.

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
   - `blocked` — a refused command, the environment or the budget stopped it.
     The report's `stopped by …` line says which, and the line under it is the
     command that continues it. Tell the user both, in those words. Its work
     so far is on its branch, marked unverified. When the user grants what was
     refused, run that command (`cauce resume <id> --allow "<the refused rule>"`).
     It is not a new task, and you never redo it by hand.
   - `failed` — every cell on the ladder tried. Report what each attempt found
     (`cauce show <id>`); do not try it yourself in this context.

   Use only the commands `cauce --help` lists; there is no `cauce continue`.

5. **Memory.** Before debugging, `cauce memory search "<symptom>"` shows fixes
   already tried against similar problems, in any repository. When a fix is
   confirmed or refuted outside a worker, record it:
   `cauce memory record --problem "<title>" --fix "<what changed>" --outcome worked|failed --why "<why>"`.
