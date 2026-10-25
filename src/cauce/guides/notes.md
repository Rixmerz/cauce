# The project's notes, and when to compact

**What this project knows.** Before rebuilding context from the code —
above all after a compaction — ask its notes: `cauce recall "<question>"`
(add `--topic business|code|decisions|conventions|environment` or
`--path <file>`). A note marked TO REVIEW describes code that changed since:
check it before relying on it. When the person tells you a durable fact
about the project (a business rule, why something is built a certain way, a
convention, how to run it), keep it: `cauce note "<fact>" --topic <t>`. A rule
every change must meet (a policy, a limit) is a constraint: `cauce note "<rule>"
--constraint`; every worker is told it as a requirement.
Workers file what they learn on their own; do not copy their reports in.
**Compacting is your call.** With the `compact` setting at `orchestrator`
(the default), cauce tells you when it sees a good moment (a large
context, a break between tasks, the cache warm), with its reasons; you
decide. Call cauce's `compact` tool when the work at hand no longer needs
this conversation's detail: right after work you sent ended and you told
the person, after a merge, before starting something unrelated. Do not
compact while something in flight still needs the detail (a review you are
half through, a decision the person has not made yet); answer the advice in
one line instead. In `keep`, put only what cauce's own digest would miss
(the person's requests, open work and branches it keeps itself). It runs
when your turn ends, so finish the turn with what the person needs now.
When nothing you sent is still running, no ending will start your next turn
after it: put in `then` what you go on with (as you would ask it yourself,
naming task numbers and branches), and once the compaction finished it
reaches you as your own prompt. Leave it out when the next step is the
person's. A
message that opens `/compact [cauce-compaction #N]` is a compaction's
instructions, never a request.
