# Chains of steps: when to run a workflow, and how a run goes on by itself

**Cut work where context changes hands, never by role.** The phases of one
task (plan, build, test; reproduce and fix) are one worker's: the one that
builds a feature writes its tests. What earns its own step is what must not
share that context: a review, which reads blind (the request and the diff),
and fixing what a review confirmed. That chain is a workflow, not one you
relay: run `cauce workflow list` and pick the one that fits (`feature`,
`bugfix`, `review-fix`, `dual-review`, `panel`), or the one the person named. Then:

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
