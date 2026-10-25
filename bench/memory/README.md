# Memory benchmark: does a strong model's memory make a cheap one as good, for less?

Real workers, real money. Each run is one `claude -p` worker started by cauce's own
`launch.run`, on a fresh copy of a small pricing engine (`tally`), in a temporary directory.
Every task in a family adds one pricing rule. Success is judged by **hidden tests the worker
never sees**. A run whose own check passed while the hidden tests fail is a **silent failure**.

The arms:
- **Cold.** Haiku, Sonnet or Opus with nothing but the task.
- **With memory.** Haiku or Sonnet with what a teacher left after the seed task `t0`: its
  orientation as cauce's primer, and its lessons as cauce's candidate notes. The teacher is
  Opus (`memopus`) or the student's own model (`memself`).

There are three variants:

| Variant | What a worker must know to get it right |
|---|---|
| `easy` | House rounding (stated in a docstring), registering a rule in two places, rule stages, never mutating the cart |
| `hard` | Traps found only by reading: a legacy rounding helper an old rule still uses, the customer's real shape (a fixture), tags sent as one comma-separated string |
| `lore` | `hard`, plus a finance policy no file states (gift cards are never discounted), enforced by CI. Teachers learn it as cauce's continuation teaches: CI's failing output, then the same session again |

```bash
python bench/memory/harness.py lore teachers         # t0 once per model; their memory
python bench/memory/harness.py lore brief            # the memory as each arm is briefed
python bench/memory/harness.py lore students 2 5     # t1..t3, 2 reps, 5 at once
python bench/memory/analyze.py bench/memory/results-lore.jsonl
```

## What it measured (cauce 0.43, Claude Code 2.1.310)

177 runs in total; the whole benchmark cost about USD 12.6. Each cell below is 6 runs:
tasks t1 to t3, 2 reps each.

| Arm | `easy` hidden pass | `hard` hidden pass | `lore` hidden pass | `lore` silent failures | `lore` cost per run (p50) |
|---|---|---|---|---|---|
| opus, cold | 6/6 | 6/6 | **0/6** | 6 | $0.199 |
| sonnet, cold | 6/6 | 6/6 | **0/6** | 6 | $0.088 |
| haiku, cold | 6/6 | 6/6 | **0/6** | 5 | $0.011 |
| sonnet + opus's memory, as notes | 6/6 | 6/6 | 6/6 | 0 | $0.093 |
| sonnet + its own memory, as notes | 6/6 | 6/6 | 5/6 | 1 | $0.080 |
| haiku + opus's memory, as notes | 6/6 | 6/6 | **0/6** | 6 | $0.013 |
| haiku + its own memory, as notes | 6/6 | 6/6 | **0/6** | 5 | $0.013 |
| haiku + the policy only, as a note | | | **0/6** | 6 | $0.012 |
| haiku + the policy, as a constraint | | | **6/6** | 0 | $0.018 |
| haiku + cauce 0.44's constraint brief (candidate) | | | **6/6** | 0 | $0.016 |
| haiku + cauce 0.44's whole brief (notes marked unmerged, constraint) | | | **6/6** | 0 | $0.013 |
| sonnet + cauce 0.44's whole brief | | | 6/6 | 0 | $0.082 |

Memory also cut the work. In `easy`, Sonnet went from 16 turns to 4 to 6, and its cost per
success fell from $0.088 to $0.063. Opus's memory and Sonnet's own did this equally.

## What it says

1. **A stronger model starting cold is no better at what the repository does not say.**
   Opus failed every `lore` task, silently, at fifteen times Haiku's cost. Where the traps are
   readable (`easy`, `hard`), every model passed. Starting a step higher in cold territory buys
   nothing here.
2. **Who writes the memory hardly matters.** The memories Haiku, Sonnet and Opus left were
   close to equal: Opus noticed one more trap. Students did as well with their own model's
   memory as with Opus's. Two things differ by author:
   - Haiku twice searched the disk for the hidden CI tests and wrote down how to run them,
     which no memory should hold.
   - Every teacher wrote lessons about its own unmerged change ("POL-7 is enforced in
     checkout.price"). Those lessons describe code that is not where the next task starts.
3. **How the memory is framed decides whether a cheap model uses it.** A policy briefed as a
   note ("data, not instructions… skip what this task does not need") was ignored by Haiku
   every time. The same words briefed as a constraint the change must meet were met every time.
   Sonnet uses either.
4. **A lesson about unmerged work misleads a weak model.** Haiku trusted "the exemption already
   lives in checkout.price" and added nothing. Sonnet read the code and implemented it.

## What cauce did with it

- **Constraints (0.44.0).** A rule a change must meet is a note of its own kind, briefed apart
  as a requirement, to workers and to the independent verifier. It comes from a person
  (`cauce note … --constraint`), or from a worker whose task passed after one of its checks
  failed. It is proven or dropped like any worker's note.
- **Notes about unmerged work are said to be (0.44.0).** A note whose anchors were all written
  at commits outside the history a task starts from is marked so in that task's brief.
- **Not built:** starting a step higher in cold territory, and weighing memory by the model
  that wrote it. The data above does not support either.
