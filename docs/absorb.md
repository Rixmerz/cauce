# What cauce takes from tasky and muscle-memory

Both are predecessors. cauce takes their **ideas**, rewritten inside its own
loop — classify, launch, check, record, learn — and drops what overlaps it,
what does not serve the goal, and what was fragile. No code is ported.

The goal every idea is measured against: route each task well, spend the
minimum, never repeat a fix that failed, have capabilities used without the
model having to remember them, and keep one entry point.

## tasky

Audited in three parts: capture, queue and workers; memory and history; the
dashboard and the architecture map.

### Kept, redesigned for cauce

| Idea | Why it serves the goal | Where it lives in cauce |
|---|---|---|
| A typed prefix (`++`) that queues work at zero tokens | work arrives faster than it can run; queuing it must not cost a turn | `UserPromptSubmit` blocks the prompt and stores a queued task |
| One serial lane per repository that **pauses** when a task fails or disappears | the next task never builds on a broken state | `lanes` table; the dispatcher skips paused lanes |
| A dispatcher that drains lanes | queued work runs in workers, not in the orchestrator's context | `cauce work` |
| A cap on unattended consecutive tasks, reset only by a human | an unattended loop is bounded | `cauce work --max` |
| A parent is not done while a child it spawned still runs | a result is not reported before the work under it ends | delegation tasks with `parent_id` |
| A pinned model is recorded as the person's choice and never counts as router evidence | history must teach the router only what the router decided | `tasks.pinned`; `landings` ignores pinned tasks |
| A preview of model × effort before launch | the person sees the cost of a choice before paying it | `cauce route`, the UI's queued column |
| A sweep that marks running work with no sign of life as interrupted | stuck rows lie about what is running | pid liveness, run by hooks and the UI server — never by a browser poll |
| Fixes carry the window they were trusted in, and a solved problem can come back | "believed right, later shown wrong" is the most valuable dead end | `fixes.believed_from`, `fixes.invalidated_on`, problem state `recurring` |
| A verdict anchored to a commit | evidence a person can check | `fixes.commit` |
| Token spend per model, from the session's own log, fed back into routing | the matrix should be calibrated on what work really cost | `usage` ingested from transcripts at `Stop` |
| Dead ends ranked by relevance to the task, each with what worked instead | already in cauce; kept as the rule | `Store.dead_ends(query)` |
| The dashboard's security envelope | a local server that can launch paid work must not be reachable by a web page | the UI server (`docs/ui.md`) |
| Attention states as the board's columns | the board answers "what needs me", not "what exists" | the UI's board |

### Dropped

| tasky feature | Why |
|---|---|
| Its router, worker launcher and supervisor | cauce's are stronger: escalation, budgets, turn ceilings, worktrees, enforced tools. tasky's router also probably overrode a person's chosen model (its `--model` was appended after the user's). |
| Prompt capture, follow-up folding, nudges, interrupted turns | cauce already does it |
| Fork with context | replays the whole conversation: more spend, not less |
| The Focus Cards output style | a reply format, not routing or spend |
| Spec readers, import graph, quick diagram | livespec covers them |
| Draw with Archify, the mind map, cards as a second model pass | cosmetic, or a paid pass over text a model already wrote |
| Smart search with Haiku | the asking agent can search itself; FTS is free |
| Stale *titles* and language detection | cosmetic |

### Deferred

| Idea | Waits for |
|---|---|
| Import past sessions, idempotently | the transcript reader in phase B |
| A model-distilled history (problems inferred from finished tasks), accepting only claims that cite a task shown and quote it verbatim | costs tokens; opt-in, after the importer |
| A scrubbed local copy of every message, searchable after Claude Code deletes logs at 30 days | storage and scrubbing rules |
| Areas: a human vocabulary over livespec's code regions | the repo map screen |

### Defects not to carry over

Found in the audit and named here so the redesign does not repeat them: a GET
that also runs the scheduler and the stale sweep, so cleanup happens only while
a tab is open; a sync lock that is never checked for staleness; a history
cursor that skips tasks still running when it advanced; a search that limits
rows before filtering by repository; an environment variable (`TASKY_TASK_ID`)
inherited by everything a worker runs; secrets scrubbed in one table and sent
verbatim from another; three copies of the worker-spawn code; a terminal board
that regex-matches a sentence the MCP tool returns.

## muscle-memory

A TypeScript monorepo: a logger of tool-call signatures, a miner of repeated
sequences, proposals, a builder that compiles a proposal into a hook, an
installer with a kill switch, and a classifier backed by a local Laya model.

| Idea | Verdict | In cauce |
|---|---|---|
| Record every tool call as a *signature* (`bash:pnpm-test` + an argument hash), never arguments, contents or prompts | **keep, always on** | `PreToolUse`/`PostToolUse` append to a per-machine log; workers record too |
| Mine repeated sequences; gates are boolean, the score only ranks | **keep** | `cauce habits` |
| Per kind of task, the sequences that precede a pass | **new, from the same data** | a "known good sequence" line in the worker's brief: fewer turns |
| Turn a sequence into a hook — only with a person's approval, behind a kill switch, demoted after three failures | **keep the rule exactly** | `cauce habits propose`; installing is always a human command |
| The classifier on Laya | **drop** | rules + Haiku already route; Laya needs a 1–3 GB Python install, never fired in tasky's chain, and its own README says it is weak on bare commands |

Recording is on everywhere because it records nothing sensitive; a
repository can opt out. *Installing* automation is never automatic: a hook
that runs itself in someone's settings is the one thing that always needs a
person.

## Order

1. **Phase A — work flow:** queue, lanes, dispatcher, stale sweep, pins,
   delegations.
2. **Phase B — memory and spend:** trust windows, recurring problems, commit
   evidence, token usage per model, spend in routing.
3. **Phase C — habits:** signature logger, miner, recipes in the brief,
   approval-gated proposals.
4. **Phase D — the UI** (`docs/ui.md`).
