---
description: Make, change, copy, run and watch cauce workflows — saved chains of tasks that go on by themselves, step after step
argument-hint: [what the person wants: run X on Y, make a workflow for Z, change step S, where is run N]
---

A cauce workflow is a saved list of steps. Each step is a task text with the
steps it waits on. A run queues the first steps. When a step passes, cauce
queues the steps that waited on it, on the commit it kept, and tells them what
it found. You start a run and watch it; you never relay a step's result to
the next one.

Request: $ARGUMENTS

## Find one

```bash
cauce workflow list              # every workflow, nearest scope first
cauce workflow show <name>       # its steps, inputs and where it is saved
```

There are three scopes, and the nearest one wins:

- `project`: `.cauce/workflows/` in this checkout, shared through the repository.
- `user`: the person's cauce home.
- `bundled`: the templates `feature`, `bugfix` and `review-fix`.

A workflow in a nearer scope hides one of the same name further away.

## Make or change one

Start from the closest existing workflow rather than from nothing:

```bash
cauce workflow new <name> --from <existing> [--scope project|user]
cauce workflow copy <source> <target>
cauce workflow new <name> --description "..."      # one empty step to fill
```

Change it one step at a time. Each change is checked before it is written:

```bash
cauce workflow step add <name> <step> --prompt "..." [--after <step>] [--needs a,b] \
    [--kind <kind>] [--verify "<check>"] [--start <model/effort>] [--budget-usd N] [--max-turns N] \
    [--memory note:12,topic:zones,problem:7]
cauce workflow step set <name> <step> --prompt "..." [--clear verify]
cauce workflow step rm <name> <step>
cauce workflow validate <name>
cauce workflow rm <name> --scope user|project
```

To rewrite a whole definition, get it with `cauce workflow show <name> --json`,
change the JSON, and write it back with `cauce workflow save <file> --replace`.
`cauce workflow edit <name>` opens it in the person's editor.

How to write the steps:

- **Prompts:** write each one so a stranger could do it. `{goal}` and any
  other name listed under `inputs` is filled in at run time.
- **Order:** a step without `needs` waits on the step before it. `needs: []`
  makes a step a first step. Two steps that need the same step run side by
  side when the dispatcher finds them independent.
- **Reading steps:** a review or a plan (`kind` `review-routine`, `plan`,
  `explore`) changes nothing. It reads the work of the steps before it.
- **Writing steps:** each one starts from the commit the nearest writing step
  before it kept.
- **Checks:** give a writing step a `verify` command when the repository has
  one. cauce runs it itself.
- **Memory:** a step can carry cauce memory into its brief, for considerations
  that step must keep:
  - `--memory note:<id>` (or a bare id) for one project note
  - `topic:<name>` for every live note in a topic
  - `problem:<id>` for a problem with every fix tried on it

  Find the ids with `cauce recall "<question>"` or `cauce memory search
  "<symptom>"`, and remove them again with `--clear memory`. Notes are read
  only from the project the run is in. A run whose memory cannot be read there
  does not start.
- **No grants:** a definition cannot hold permission rules, approvals or
  capabilities. Those are the person's flags on `run`.
- **Changing a bundled template:** it is copied to the user scope first, and
  the template itself stays as it was.

### While it is in use

A workflow that active runs use (in any project, as happens with a global
one in the user scope) is held. A change you make to it is kept as a
**pending revision** and applies by itself when the last run using it ends
or is cancelled. Further changes build on that pending revision.

```bash
cauce workflow show <name>        # its version, the runs using it, a pending change
cauce workflow pending [<name>]   # what waits, and for which runs
cauce workflow pending <name> --apply|--drop
```

Pass `--now` to a change only when the person wants it at once. The runs
keep the copy they started with either way. A held workflow is removed only
with `--force`.

## Run one

Running spends money, so do it only when the person asked for this work:

```bash
cauce workflow run <name> "<value of its one input>"
cauce workflow run <name> --input goal="..." --input area="..." [--budget N] [--allow RULE] [--allow-approval]
```

The run starts the repository's dispatcher. Then, without you:

- Each step runs in its own worker, with its own cell, check and escalation.
- The next step starts when that one passes.
- The session that started the run hears of it once: at its end, or when a
  step waits on the person.

## Watch one

```bash
cauce workflow status [<run>] [--all] [--json]
```

It shows each step's state, its task and cell, the steps running now, the
steps waiting on the person, and the branch that holds the work so far.
`cauce overview` lists the runs of every project. `cauce show <task>` shows
one step in depth.

When a step stopped:

- **Read why:** `cauce show <task>`.
- **Can go on:** `cauce resume <task>`, or `cauce workflow retry <run> <step>`
  to queue it again as a new task. Either way the run goes on by itself once
  the step passes.
- **Should stop:** `cauce workflow cancel <run>`. No further step starts.

Some things only the person may clear: a refused command, an approval, a
branch to merge. Tell them; do not clear it for them.
