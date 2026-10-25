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
