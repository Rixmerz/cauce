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
- `bundled`: the templates `feature`, `bugfix`, `review-fix` and `dual-review`.

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
    [--kind <kind>] [--role <role>] [--surfaces 'src/api/**,docs/**'] [--verify "<check>"] [--start <model/effort>] [--max-turns N] \
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
- **Roles:** `--role <name>` runs a step as one role of the project's team
  (`cauce team show`), say `security-auditor` for an audit step. A role the
  project's team does not have is not used: the team picks, and the plan says
  so.
- **Surfaces:** `--surfaces` bounds where a writing step may change files. A
  change outside them fails the attempt and is undone. Steps with disjoint
  surfaces run side by side.
- **Review ledger:** after review steps, `--ledger refute` decides the
  findings only one review saw, `--ledger fix` fixes only the confirmed ones,
  and `--ledger recheck` checks each one. Reviews never vote. A row still open
  gets a second round, then the run waits on the person. `dual-review` is
  built this way.
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
- **No grants, no caps:** a definition cannot hold permission rules,
  approvals or capabilities (those are the person's flags on `run`), and no
  step has a dollar limit: its room is `--max-turns`. A `budget_usd` an older
  definition holds is read and ignored.
- **Cut where context changes hands, never by role:** one step builds with its
  tests (or reproduces and fixes); a review is its own step and reads blind (the
  request and the diff, not what earlier steps said; one with no diff to read,
  such as a plan's critique, gets what came before); fixing what it found goes
  through the ledger (`refute`, `fix`, `recheck`), skipped when the review is
  clean. A step gets the findings of the steps it waits on as records.
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

Running spends money, so do it only when the person asked for this work.
`cauce workflow list` shows what each workflow takes (its inputs, its memory);
check a run first with `--dry-run`, which queues nothing and says what is
missing, so the first real start is the one that runs:

```bash
cauce workflow run <name> --input goal="..." --input check="..." --dry-run
cauce workflow run <name> "<value of the one input not named>"
cauce workflow run <name> --input goal="..." --input area="..." [--verify "<check>"] [--allow RULE] [--allow-approval]
```

`--verify` is the check of every step that writes and names none of its own;
a reading step never gets it. A memory topic a step names that holds no note
yet in this project is said when the run starts, and the step goes without
it; add one with `cauce note --topic <topic> "..."` when the person has it.

The run starts the repository's dispatcher. Then, without you:

- Each step runs in its own worker, with its own cell, check and escalation.
- The next step starts when that one passes.
- The session that started the run hears of it once: at its end, or when a
  step waits on the person.

## Watch one

```bash
cauce workflow status [--all]           # one line per run: the step it is at, its attempt, last commit, files
cauce workflow status <run> [--full]    # each step: state, task, attempt, phase, commit, files
```

The inputs show on one line (`--full` shows them whole). One run shows each
step's state, its task and attempt, what a running worker is doing, the commit
a done step kept and the files it touched, the steps waiting on the person,
and the branch that holds the work so far.
`cauce overview` lists the runs of every project. `cauce show <task>` shows
one step in depth.

When a step stopped:

- **Read why:** `cauce show <task>`.
- **Can go on:** `cauce resume <task> --unattended`, or `cauce workflow retry
  <run> <step>` to queue it again as a new task. Either way the run goes on by
  itself once the step passes. Leave `--unattended` off only for a resume that
  carries what the person just granted (`--allow`, `--allow-approval`, `--keep`).
- **Should stop:** `cauce workflow cancel <run>`. No further step starts.

Some things only the person may clear: a refused command, an approval, a
branch to merge. Tell them; do not clear it for them.
