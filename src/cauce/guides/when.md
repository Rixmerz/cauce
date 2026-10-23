# When a worker earns its cost, and how to split work

**Do it yourself when it fits in what this session already holds** — a
question, a clarification, a lookup a few searches settle, or a small change
of a few lines in files you have read. One agent with the right context is
the cheapest worker there is: a hand-off costs a setup, a brief and a report,
and loses what you know. A worker earns its cost when:
- the work would fill this context with reading (a wide exploration, a
  debugging hunt across the code, a large change);
- it has independent parts that can run side by side (below);
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

**Split it when it has independent parts.** One `cauce run` per part, each
with its own `--surface`. Parts whose surfaces cannot touch run at the same
time (each gets its own worktree); run them as background commands and
collect the reports. A part that builds on another goes `--after` it. A
build that lists six items or more (40 words or more), is the largest size, or names three
pieces in folders of their own is refused before any worker starts (exit 5,
`not started: this request is several tasks`): split it as it says, or
send it as `cauce workflow run feature --goal …`, whose plan splits it.
`--whole` runs it as one task when you know it is one. Two parts without
surfaces that change the same file conflict when both merge: an ending
that says `other work here changes the same files` names them; merge one
first.
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
