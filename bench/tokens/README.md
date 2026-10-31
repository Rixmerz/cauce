# Token benchmark: would a lexicon and references make briefs cheaper?

The idea under test, in three layers:

1. **References instead of copies.** Kept items (notes, primer, dead ends, earlier attempts, a check's output,
   the session's thread) go in a brief as an id and one line, and the worker fetches the full text with a tool
   when it needs it. The request, its `S#`, constraints, a fix's ledger rows and the verifier's blockers stay
   verbatim.
2. **A lexicon of phrases** mined from cauce's own text. One-word aliases go in the brief, and their
   definitions go in the stable system prompt. A phrase is admitted when
   `(tokens(phrase) - tokens(alias)) x frequency - tokens(definition) > 0`.
3. **Bounded output.** Shorter fields in the result block.

The rule fixed before anything ran: T1 must show at least 10% fewer tokens, or nothing after it (comprehension,
cache, the A/B on hidden tests, production arms) is run.

Real workers, real money. `collect.py` runs `cauce run` and a `review-fix` workflow on fresh copies of
`bench/memory/hard`, with `tap.py` standing in for `claude` on PATH. Each worker call is recorded: its argv,
its appended system prompt, its stdin (the brief and the result contract) and the CLI's own usage.
`analyze.py` counts every text with the real tokenizer. It asks the CLI once with the text and once with a
single character, and takes the difference of the first request's context. It then scores each layer against
what the attempt really cost.

```bash
python bench/tokens/collect.py 1 6                   # memory and cold arms (notes, a constraint, a dead end)
python bench/tokens/collect.py 1 3 hidden            # CI behind a script: first tries fail, later briefs carry it
python bench/tokens/collect.py 2 2 review            # review-fix on a commit with planted bugs
python bench/tokens/analyze.py bench/tokens/tap-*.jsonl
```

## What it measured (cauce 0.60, Claude Code with Claude 5 models)

This covers 34 worker calls: 17 first attempts, 6 later attempts, 2 continued sessions and 9 independent
checks, from 9 tasks and 2 `review-fix` runs. The workers cost $2.63, and the classifier and notes calls
another $0.02. Costs are in base input prices: an hour's cache write is 2x, a cache read 0.1x, and output 5x.
The brief is in every request of an attempt, so it is written once and read on every later request.

**T0: where an attempt's cost goes.**

| | Share of the attempts' cost |
|---|---|
| The brief (task, context, result contract) | **8.3%** pooled; median 2,832 tokens, 7 requests per attempt |
| The role's appended system prompt (`implementer`: `engineering-baseline`, `ponytail` and 13 language-skill pointers) | **8.4% to 24.9%**; median 8,534 tokens, 3x the brief. The share depends on whether workers share its cache |
| Output | 28.8% (the final answer's text alone: 5.8%) |
| The rest: Claude Code's own prompt and tools, tool results, the model's turns | the remainder |

**T1: each layer, against the whole attempt.**

| Layer | Brief tokens | Cost of the attempts |
|---|---|---|
| References, no fetch (best case) | −4.4% overall; −8.7% median where there was something to reference (14 of 34) | **+0.19%** saved |
| References, one fetch of everything (worst case) | — | **−2.1%** (costs more); later attempts −7.1% |
| Lexicon, scored as designed (corpus frequency) | +326 tokens per request | **−0.9%** (costs more) |
| Lexicon, scored per request | −576 tokens per request, all of them in the role's skill text | at most +1.6% |
| Dedup: what a brief says twice, nothing left out | −2.5% overall; up to 535 tokens (−15%) in a workflow's fix and recheck steps | +0.19% |
| Bounded output (ceiling: the final answer's text) | — | under 5.8% even if it were cut to nothing |

## What it says

1. **T1 fails, so T2 to T5 were not run.** No layer comes near 10% of the brief's tokens. Against the
   attempt's cost, the best layer saves under 0.2%.
2. **The lexicon as designed loses.** Its formula counts a phrase's frequency over the whole corpus. But a
   definition is paid in every request, and an alias saves only what that same request repeats. Scored per
   request, the only phrases that pay are repeats inside the `*-rules` skill pointers ("Use ONLY when the file
   under edit or review is …", 13 times). That is text to edit, not a lexicon.
3. **References cost more than they save as soon as a worker uses them.** One fetch is a request that reads
   the whole context again (a median of 290k tokens per attempt, about 41k per request). That outweighs the
   ~300 tokens a reference saves. Later attempts, where the design expected the most, lose the most: 7.1%.
   Every point `bench/memory` made about framing still holds and was not even tested.
4. **"Do not repeat" is right, at the source, with no references.** Real briefs say things twice:
   - A fix or recheck step carries its ledger rows, then the same rows again, clipped, as its criteria `S1..S4`.
     That is about 1.4k characters.
   - A note's line prints its title, which is the start of its text, and then the text.
   - The primer repeats near-identical lines from different workers.
   - The check command is said twice.

   Removing those keeps everything verbatim once. It saves little (0.2% of cost), but it costs nothing and
   risks nothing.
5. **The lever is the role's system prompt, not the brief.** It is 8.5k tokens per worker, inlined:
   `engineering-baseline` (17k characters), `ponytail` (7k) and pointers to 13 languages' rules in a Python
   repository. Whether workers share its cache decides whether it is 8% or 25% of the cost. That is what to
   measure next: per-request `cache_creation` for the system prefix across workers of one role, and a brief
   that lists only the rules skills for the languages the task touches.

## Limits

- 34 calls, one small Python repository, one bundled role. Other roles' system prompts and briefs with a
  long session thread or many dead ends were not sampled; references would save more there and cost the
  same per fetch.
- Tokens were counted with Haiku's tokenizer. The cost multipliers are list ratios, not this account's bill.
- The fetch cost is a modeled bound (no fetch, one fetch of everything), not measured worker behaviour.
- The lexicon was mined from these briefs alone. A corpus of thousands would add phrases, but not change
  the per-request arithmetic that sinks it.
