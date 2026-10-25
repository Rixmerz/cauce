# Review benchmark: two blind reviewers with one purpose, or a classifier before a quality check?

Real workers, real money. Each run is a whole review chain of `claude -p` workers started by
cauce's own `launch.run`, on a fresh copy of a change to a small pricing engine (`tally`, the
base of `bench/memory`). The change has bugs planted in it. A chain's confirmed rows are matched
to the planted bugs by file, line and words (`analyze.py`; the bugs live packed in `truth.tgz`,
so no worker reading the disk finds them).

```bash
python bench/review/harness.py run <plain|volume> single,dual,split,funnel 3 4
python bench/review/analyze.py bench/review/results-volume.jsonl --rows
```

The arms are each a chain cauce runs or could run:

| Arm | Chain |
|---|---|
| `single` | one blind review (opus/high), then a refuter on the rows it only read (`review-fix`) |
| `dual` | two blind reviews with one purpose (sonnet/high, opus/high), then a refuter (opus/xhigh) on what only one saw and only read (`dual-review`) |
| `split` | two blind reviews with different purposes: a sonnet that sorts the change and shows what it finds, beside the opus reviewer; then the refuter |
| `funnel` | a sonnet classifier sorts the change (`mechanical`, `covered`, `uncovered`) and shows what it can; an opus quality check, not blind, decides each of its rows, checks its classes, reviews what it left and is the last word: no refuter |

The variants:

- `plain`: 237 changed lines in 16 files, with nine bugs (rounding, a case-sensitive lookup on
  data, a substring match, an off-by-one, a rule never enabled, negative amounts printed wrong,
  stacked discounts, an unquoted CSV, a remainder lost).
- `volume`: the same change inside a refactor that moves 24 rules into a folder per stage and
  renames their methods. That is 1,221 diff lines in 69 files. Five more bugs hide in hunks that
  look mechanical: a percent changed in a move, `>=` turned to `>`, a substring test, a stage
  changed, and a rename that missed one file.

## What it measured (cauce 0.44, Claude Code 2.1.310)

One run per arm on `plain`; every arm caught 9/9. These are the `volume` results, three runs per
arm, 14 bugs each. The cost and the critical path are medians per chain. The path counts the
blind reviews side by side and every other step after the one before it.

| Arm | Caught | Hardest bug (B3: tier case vs the data) | Cost | Path | Refuter ran | Refuted |
|---|---|---|---|---|---|---|
| single | 40/42 | 1/3 | $0.65 | 132 s | 1 of 3 | 0 |
| dual | 40/42 | 2/3 | $1.02 | 152 s | 3 of 3 | 0 |
| split | 41/42 | 2/3 | $1.01 | 140 s | 1 of 3 | 0 |
| funnel | 40/42 | 1/3 | $0.90 | 208 s | (none) | |

In all six two-review chains (`dual` and `split`), the sonnet reviewer found **no planted bug
that the opus one did not find too**. Its rows only repeated rows the opus reviewer had. B3 came
from the opus reviewer each time the pair caught it.

In the funnel, the classifier found 11 to 13 of the 14 bugs, and the quality check added one to
two. The check did its job on the classes: twice it caught a file the classifier called
mechanical that was not. Even so, it cost as much as a blind opus review ($0.66 against $0.52 to
$0.88). Deciding a row means reading the code the row is about, the same work as finding it. It
also caught the hardest bug no more often than one reviewer alone, and it is the slowest arm,
because its two stages cannot overlap.

## What it says

1. **The second blind reviewer paid for nothing here.** Recall did not move. It cost about 60%
   more and was slower than one strong review, and the refuter it calls in refuted no row.
   cauce's default chains (`feature`, `bugfix`, `review-fix`) already run one review;
   `dual-review` and `panel` stay as templates a person chooses.
2. **A classifier does not shrink the quality check's work the way it does on a packing line.**
   An inspector checks a sorted crate in seconds. A model checking a row reads the same code it
   would read to find it. Two blind readers keep their errors independent; a checker that is
   handed the map follows it.
3. **The pipeline is where the analogy holds.** On a packing line no stage waits for the one
   before it to finish. In cauce, measured over its own store, the handoff from one step to the
   next takes 1 to 3 seconds. The exception was a review step that waited 30 minutes with the
   machine idle: it had been queued while the lane's last dispatcher was leaving.

## What cauce did with it (0.45.0)

- **A start is never lost while the last dispatcher leaves.** A start leaves word before it asks
  whether a dispatcher runs. A dispatcher that read its queue before that word starts a fresh one
  once its lock is released. `cauce doctor` names a pickup that waited with nothing running in
  its lane (`pickup-nobody-saw`).
- **`cauce stats` says what each blind review added beside another**: the rows it put on the
  ledger, the rows no other review saw, and how many of those were kept. So whether a template's
  second reviewer pays is read from a person's own runs, not from this benchmark.
- **Not built:** the funnel and the split reviewers. The data does not support either.

The caveat: one family of small changes, three runs per arm. The bugs were findable by one strong
reader. On a change where they are not, a second blind reader may pay, and the stats line above
is how that would show.
