# Speed benchmarks: where cauce's wall time goes, and what shortens it without costing quality

Real workers, real money. Each benchmark measures one thing cauce could do faster, beside what it
does today, and keeps the result that decides it.

```bash
python bench/speed/overlap.py <commit> 3            # cauce's check beside the verifier, or after it
python bench/speed/build.py run sonnet/high,sonnet/xhigh,opus/high,opus/xhigh 3 4 && python bench/speed/build.py report
python bench/speed/plan.py run opus/high,opus/xhigh,opus/max 2 3 && python bench/speed/plan.py report
python bench/speed/handoff.py run self,cauce 4 4 && python bench/speed/handoff.py report
python bench/speed/dispatch.py current,named2 3 && python bench/speed/dispatch.py report
python bench/speed/build.py run sonnet/high,sonnet/high+batch 4 4      # a `+batch` arm asks for parallel calls
```

## Where the time went (this machine's store, before 0.45)

| Step | Minutes in all | What weighs |
|---|---|---|
| build | 472 | its worker (381) |
| fix | 177 | its independent verifier (44), its check (111 s per attempt at the median) |
| review | 111 | its worker |
| recheck | 66 | its worker (now decided by the fix's verifier where it answered every row) |
| refute | 35 | its worker |

Over 53 attempts that the independent verifier checked, cauce's check (p50 61 s) and the
verifier (p50 170 s) ran one after the other: 237 minutes. Run side by side, they would have
taken 66 minutes less.

## A: cauce's check beside the independent verifier (`overlap.py`)

The setup is a worktree of cauce at a real commit (`206ec54`), cauce's full suite as the check,
and a real verifier (`verifier_brief`, opus/high, read-only) on that commit's change. Each of
the three reps ran one after the other, as cauce did before 0.45, and then side by side, on a
machine with 4 CPUs.

| Rep | One after the other | Side by side | Check alone | Check beside the verifier |
|---|---|---|---|---|
| 1 | 792 s | 260 s | 262 s | 260 s |
| 2 | 822 s | 478 s | 270 s | 263 s |
| 3 | 1,235 s | 533 s | 260 s | 263 s |

The check was not slowed by the verifier beside it. The verifier passed in all six runs and
found the same things (2 to 3 findings). Side by side saved the whole check or more every time:
the median went from 822 s to 478 s (−42%).

cauce does this from 0.45: the `overlap` setting, `orchestrate.Ahead`.

## B: which cell gets a build done soonest, and right (`build.py`)

Each run is one worker on a fresh copy of the pricing engine, and hidden tests the worker never
sees judge the result. A build is right only when every hidden test passes. There were three
runs per cell for each build.

**Medium build:** three pricing rules, a receipt and a CSV export (7 hidden tests).

| Cell | Right | Wall p50 | Turns | Cost p50 |
|---|---|---|---|---|
| sonnet/high | 3/3 | 57 s | 6 | $0.14 |
| opus/high | 3/3 | 118 s | 10 | $0.43 |
| sonnet/xhigh | 3/3 | 170 s | 40 | $0.45 |
| opus/xhigh | 3/3 | 192 s | 24 | $0.77 |

**Harder build:** the same feature, plus moving 24 rules into a folder per stage and renaming
their methods everywhere (10 hidden tests).

| Cell | Right | Hidden tests | Wall p50 | Turns | Cost p50 |
|---|---|---|---|---|---|
| sonnet/high | 3/3 | 30/30 | 84 s | 12 | $0.23 |
| sonnet/xhigh | 3/3 | 30/30 | 276 s | 57 | $0.81 |
| opus/high | 2/3 | 29/30 | 189 s | 16 | $0.82 |
| opus/xhigh | 2/3 | 29/30 | 382 s | 49 | $1.72 |

More effort tripled the wall time and passed no more hidden tests. sonnet/xhigh, where every
feature started before 0.45, was right as often as sonnet/high, at 3 to 4 times its time. From
0.45 a feature starts at sonnet/high. A feature classified as high complexity starts a rung up,
and a repository's history and its calibrated standard still raise the start where its features
needed more.

## D and its cousin: starting what waits before its predecessor is verified

**Work held on a step whose verifier is still running.** No verified step in this store had
anything waiting on it in the current templates:
- A build or repair followed by a blind review runs no verifier.
- A fix's re-check is now decided by that same verifier.
- Work held with `--after` on another task happened once.

Not built.

**A review started before the build's own check finished.** The check failed 12 times in 50
after a worker claimed a pass. Starting the review early would save the check's median 65 s on
builds of 15 to 40 minutes, and throw away one review in four. Not built.

## C: how much effort a plan for parts needs (`plan.py`)

The plan step of `feature-parallel` was asked for the build of `build.py`, two runs per cell.
cauce's own parts reader (`launch.read_parts`, strict) judged each answer.

| Cell | Valid | Parts | Wall p50 | Cost p50 |
|---|---|---|---|---|
| sonnet/high | 2/2 | 4, 4 | 59 s | $0.18 |
| opus/high | 2/2 | 3, 3 | 93 s | $0.29 |
| opus/xhigh | 2/2 | 3, 3 | 189 s | $0.55 |
| opus/max | 2/2 | 3, 3 | 510 s | $1.56 |

opus/high, opus/xhigh and opus/max drew the same three parts (rules with checkout, receipt,
export). From 0.45 a plan starts at opus/high, where it started at opus/xhigh. sonnet/high split
the work another way (checkout apart from the rules). Whether those parts build as well was not
measured, so it is not the start.

## Where the time is now

Over this store's 1,148 minutes of runs:

| Phase | Minutes | What shortens it |
|---|---|---|
| the workers' own turns | 885 | the start cells: features at sonnet/high, plans at opus/high |
| the independent verifier | 171 | now beside cauce's check |
| cauce's check | 81 | now behind the verifier, wherever one runs |
| pickups | 30 | the lost wake-up, fixed |
| notes | 8 | |
| the plan | 1.5 | |

What is left is mostly each turn's model latency, which cauce does not set. Measured and not
built:
- skipping cauce's check when the worker ran the same command last: the overlap already hides it
  wherever a verifier runs, and the rest is about 4% of a build;
- reviews at lower effort: quality lost;
- splitting a review of a change with no parts by its folders: slower and dearer.

## E: the worker's own full check before it reports (`handoff.py`)

Worker transcripts on this machine (28 sessions, 13,123 s) spent 64% of their wall time waiting on
the model and 29% (3,793 s) in tests the workers ran themselves. Many of those runs were the whole
suite, 130 to 180 s each, three or four times in one task. The brief told a worker to run the near
tests as it goes and the full check "yourself once, at the end, before you report". Then cauce ran
the same check again.

The pricing engine got a slow suite: 12 more test files of 5 s each, 60 s in all. The worker,
sonnet/high, got cauce's own brief in two arms. `self` was the line before 0.47. `cauce` said:
"Do not run it yourself before you report: cauce runs it as soon as you do, and hands you its
failures to fix." cauce's check ran after the worker. A red check would have continued the
worker's session with its output, up to twice. Hidden tests judged each build.

| Build | Arm | Right | Green | Wall p50 | Worker's own full runs |
|---|---|---|---|---|---|
| medium | self | 3/4 | 4/4 | 216 s | 6 |
| medium | cauce | 4/4 | 4/4 | 131 s | 0 |
| harder | self | 3/3 | 3/3 | 208 s | 3 |
| harder | cauce | 3/3 | 3/3 | 152 s | 1 |

No run in either arm needed a second round, and none was less right without its own full run.
From 0.47 the brief says the `cauce` line.

## F: does the dispatcher's judgment hold independent work back (`dispatch.py`)

A queued writing task that is told to wait waits for the whole run of the work ahead, often
10 minutes or more. Haiku decides whether it starts beside that work, and its prompt said "when
unsure, answer false". Asked about clearly separate work (an invoice CSV export beside a profile page),
it waited 8 times in 8. Its reasons were "declares no surfaces" and helpers the two "might share".

Real `dispatch.decide` calls, 3 runs per pair. The pairs come in two sets. The tuning set has 14
independent pairs and 16 that collide (one needs the other's change, or both change the same
code). The held-out set has 6 and 6, written after the prompt was chosen, from real requests.

| Prompt | Set | Independent pairs started | Colliding pairs started |
|---|---|---|---|
| before 0.47 ("when unsure, answer false") | tuning | 29/42 | 0/48 |
| a wait names its collision | tuning | 42/42 | 1/48 |
| the same, and a result resting on behaviour the other changes is a collision | tuning | 42/42 | 0/48 |
| before 0.47 | held out | 14/18 | 0/18 |
| the last one | held out | 18/18 | 0/18 |

From 0.47 the dispatcher asks with the last prompt. A wait it gave is also asked again, at most
every two minutes, once the work ahead changed files it had not changed when the wait was
judged. The first judgment is often made when that work has just started and changed nothing.

## G: history kept raising where a task starts (`build.py`, the store)

A task's start rose to where half of the last tasks of its kind had passed. A task that started
high and passed there counted as a reason to start the next one as high, so a start history had
raised never came down, and the cells below were never tried again. In this machine's store:

| Kind | Passed where they started (last tasks) | Start history gave | Start from climbs only |
|---|---|---|---|
| feature | 11/14 | opus/high | sonnet/high |
| implement | 19/20 | sonnet/high | sonnet/medium |

`build.py` with the cell the fix leaves implement at, three runs per build, beside the cells
measured before:

| Cell | Medium build: right, wall p50 | Harder build: right, wall p50 |
|---|---|---|
| sonnet/medium | 3/3, 48 s | 3/3, 69 s |
| sonnet/high | 3/3, 57 s | 3/3, 84 s |
| opus/high | 3/3, 118 s | 2/3, 189 s |

From 0.48 only a climb raises a writing kind's start. A reading kind keeps the old count: its
pass is its answer, and `bench/review/effort.py` showed a review at lower effort missing the
hardest bug.

## H: an ending reaches the idle session sooner (the mod's pulse)

When work ended while the session was idle, the mod heard of it at its next poll, every 15 s
(`cauce endings` and the board: about 1.2 s of two processes each time). In this store the
mod's deliveries took 9 s at the median and 16 s at p90 from the ending. A task's ending now
touches a file beside the store, and the mod stats it every 2 s and polls at once when it
changed: within about 2 s plus the poll, with no process between endings.

## I: asking workers to make independent tool calls in one turn (`build.py`, `+batch`)

Worker transcripts here made 1.18 tool calls per turn, and 64% of a worker's wall time was the
model's own latency. Fewer turns could be faster. The `+batch` arm adds one line to the request:
make the calls that do not depend on each other in the same turn. sonnet/high, four runs per arm:

| Build | Arm | Right | Wall p50 | Turns p50 |
|---|---|---|---|---|
| medium | as briefed | 4/4 | 76 s | 5.5 |
| medium | `+batch` | 4/4 | 80 s | 30 |
| harder | as briefed | 4/4 | 100 s | 11 |
| harder | `+batch` | 4/4 | 111 s | 26.5 |

It was slower in both, so it was not built.

## J: filing notes off the critical path

A passing run filed what its worker learned (a Haiku call) before its `finished` event and before
its workflow advanced: 5.3 s at the median and 7.5 s at p90 over 107 runs in this store, on every
step of every workflow. From 0.49 a run files them after its ending and the advance; a
`notes_filed` event keeps the time it took.
