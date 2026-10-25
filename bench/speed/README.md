# Speed benchmarks: where cauce's wall time goes, and what shortens it without costing quality

Real workers, real money. Each benchmark measures one thing cauce could do faster, beside what it
does today, and keeps the result that decides it.

```bash
python bench/speed/overlap.py <commit> 3            # cauce's check beside the verifier, or after it
python bench/speed/build.py run sonnet/high,sonnet/xhigh,opus/high,opus/xhigh 3 4 && python bench/speed/build.py report
python bench/speed/plan.py run opus/high,opus/xhigh,opus/max 2 3 && python bench/speed/plan.py report
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
