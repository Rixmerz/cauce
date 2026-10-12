---
name: engineering-baseline
description: The language-agnostic rules every cauce role carries, plus the precedence rule that settles conflicts between the project's own conventions, a `*-rules` skill, and `ponytail`. Use on every code-touching task, in any language — it is what the per-language rules skills sit on top of. Also use when two loaded instructions contradict each other and you need to know which one wins.
---

# engineering-baseline

Two things live here: **the precedence rule** (which instruction wins when
loaded instructions disagree) and **the general rules** that hold in every
language. The `*-rules` skills sit on top of this file; they never replace it.

## Precedence — highest wins, no exceptions

An agent runs with several sources of instruction loaded at once, and they do
conflict. `python-rules` says "use uv"; the repo has a `requirements.txt` and a
pinned pip. `ponytail` says "stdlib first"; `typescript-rules` says "validate
with Zod". Without a stated order, the agent picks whichever it read last.

1. **The user's explicit request.** If the user asked for it, it happens —
   even when a rule below calls it a bad idea. Say the concern once, then build
   what was asked.
2. **Safety and correctness.** Injection, authz, secrets, data loss, memory
   safety. Never traded away for style, brevity, or convention-matching.
3. **The project's existing conventions.** What the repo already does wins over
   what any skill prefers. A repo on pip and Flake8 stays on pip and Flake8; a
   repo throwing exceptions does not get one `Result<T, E>` island. Convention
   drift costs more than the better tool buys.
4. **The `*-rules` skill for the file's language.** Applies where the project
   has no established position.
5. **`ponytail` minimalism.** The tie-breaker between two options that both
   satisfy 1–4: take the shorter one.

### The tooling corollary

A `*-rules` skill naming a third-party package (`Pydantic`, `Zod`, `structlog`,
`thiserror`, `chi`) is a **recommendation for greenfield code**, and greenfield
means the project has no incumbent. It is never a license to add a dependency
to a repo that already solved that problem another way. Rung 3 outranks rung 4,
and `ponytail`'s ladder — stdlib, then platform, then a dependency already in
the manifest, then a new one — governs how you satisfy the rule.

Adding a dependency is a decision, not a detail: state which rung of the ladder
failed and why, in your report.

## General rules — every language

### Errors
- Fail loudly at the boundary, not silently three layers in. A swallowed error
  is a bug that will be reported by a user instead of a test.
- Never catch broadly and continue. Catch the narrowest type you can handle;
  everything else propagates.
- Attach context when you wrap: what was being attempted, with which input.
  `"failed"` is not a diagnosis.
- An empty `catch`/`except`/`rescue` needs a comment saying why the failure is
  genuinely safe to ignore.

### Security — non-negotiable, outranks every style rule
- Parameterize every query. String interpolation into SQL, shell, HTML, LDAP,
  or a template is the finding, regardless of how trusted the input looks
  (CWE-89, CWE-78, CWE-79).
- Validate external input at the trust boundary: user input, API responses,
  environment variables, file contents, message payloads (CWE-20).
- Never commit a secret — not in code, not in a config default, not in a test
  fixture, not in a comment. Read them from the environment or a secret store
  (CWE-798).
- Never log credentials, tokens, full card numbers, or personal data
  (CWE-532, CWE-359).
- Deny by default on authz. An endpoint with no explicit check is unprotected,
  not "protected by default" (CWE-862).
- Use a CSPRNG for anything security-bearing, and a constant-time compare for
  anything secret (CWE-330, CWE-208).

Cite the CWE when you report a finding — `security-baseline` has the surface
index, the severity ladder, and the triage protocol for a dependency CVE. The
language's own footguns live in that language's `*-rules` skill.

### Dependencies
- Follow `ponytail`'s ladder before adding one.
- A new dependency needs a stated reason why the stdlib and the existing
  manifest could not do it.
- Pin versions the way the project already pins them. Never add an unpinned
  dependency to a repo that pins (CWE-1104).
- Update the lockfile in the same commit as the manifest.
- A new dependency is a new trust relationship: confirm it is the package you
  meant, not a typosquat, and never fetch build inputs without an integrity
  check (CWE-494).
- **After a generator or scaffolding CLI runs, diff the manifest and account
  for every dependency it added, by name.** They arrive unasked, and one that
  nothing imports is either dead weight or a typosquat of the alias you meant:
  a widely used component CLI emitted `import { cn } from "cn"` across sixteen
  files and installed the unrelated `cn` package to satisfy it. Its output is
  not reviewed code — inherit it deliberately or not at all (CWE-1357).
- Never dismiss an SCA advisory from memory. Quote the tool's output, establish
  whether the vulnerable path is reachable, and prefer the version bump —
  `security-baseline` has the protocol.

### Naming and structure
- Name for the reader at the call site, not for the implementation.
- No generic buckets: `utils`, `helpers`, `common`, `misc`, `manager`, `data`.
  If the name does not say what is inside, the module has no single job.
- Match the file's existing structure and idiom before introducing a new one.
- Delete dead code rather than commenting it out. Version control remembers.
- A linter at zero is never bought by widening the public API. Exporting an
  unused function to silence an unused-symbol warning announces it as something
  callers may depend on, which is worse than the warning. Delete it, or keep it
  private with a comment saying why it still exists.

### Comments
- Comment *why*, never *what*. The code says what.
- Do not narrate the change in a comment (`// added null check`) — that belongs
  in the commit message.
- Keep comments true. A stale comment is worse than no comment.
- Never describe the current state of a file you do not own. It was true when
  you wrote it, and the change that makes it false lands in someone else's
  diff — on work running beside yours, which nothing tells you about. Say what
  you depend on, not what they currently do.

### Natural language — the repository's, not the session's
- **What you commit takes the repository's language, not the session's.**
  Comments, docstrings, identifiers, test names, commit subjects and docs are
  read by whoever maintains the file next, and that reader is not in this
  session. The English rule under *Reporting done* governs the report you hand
  back — one channel, read once, by another agent. It says nothing about what
  lands in the tree, and read as though it did, it turns every repository into
  this one's convention.
- **Read the language off the files, not off the request.** The neighbouring
  module's comments and the last twenty commit subjects are the convention. A
  repository that has already answered this has answered it, and the answer
  does not change because whoever dispatched you wrote in something else.
- **Mixing is the defect, not the choice.** Half a file in one language and
  half in another costs every later reader a translation before they can tell
  whether the two halves still agree — and the halves are usually written
  months apart, which is exactly when they stop agreeing. Where there is no
  convention yet, a new repository on its first file, take the README's
  language and say in your report which one you took.
- **Never translate a file you were not asked to translate.** Rewriting
  comments or renaming identifiers into another language touches every line and
  buries the change you were sent to make inside a diff nobody can review. If
  the language a file is in is genuinely wrong for the project, that is a
  finding for your report, not a side effect of your task.

### Tests
- A test that has never been observed failing has not been shown to test
  anything. Observe it: invert its assertion, run it, confirm red, restore.
  Mutate the test you just wrote, never production code — dying mid-mutation
  leaves the tree broken.
- **A mutation that stays green is a finding, not a pass.** It says the path
  you think you covered never ran. Find out why before you report anything;
  "the mutation didn't go red" is the sentence that precedes discovering the
  test was never exercising the code.
- A suite that did not run is neither passing nor failing. A mutation that
  leaves a syntax error and reports "no tests" has proved nothing; make the
  edit valid and mutate again. Read the count, not the colour.
- When the broken path and the correct path produce the same value in the test
  environment — an empty result either way, a default that matches the real
  loader's output — comparing results discriminates nothing. Assert on the
  **call** instead, or get an environment where the two values differ.
- Test the entrypoint the system actually calls, not a helper it may bypass.
- **New code is not covered until you have seen the runner count it.** A test
  file outside the runner's include globs is not a failing test, it is an
  absent one: the suite stays green while reporting on nothing. Note the test
  count before and after; if it did not move, the runner never saw your files.
  Ask the same of the type checker and the linter — a package missing from the
  root `tsconfig.json` passes `--noEmit` without being read, which makes
  "typecheck passes" a statement about nothing.
- Never add a flag to make the suite pass or exit. A suite that will not close
  on its own is a finding about the code — a leaked handle, an unclosed
  connection — and the flag that hides it also hides the next one.
- No sleeps for async — poll or await a condition.
- Each test owns its data; no order dependence, no shared mutable state.

### Concurrency
- Every blocking operation in async code goes to a thread/executor, never on
  the event loop.
- Every spawned task is owned by something that cancels it. No orphans.
- Respect cancellation — a task that ignores its cancellation signal leaks.

### Finding things — the language server before grep

`grep` matches text. A language server resolves bindings. When the thing you
are looking for is a *symbol* — a function, type, method, field — reach for
`LSP` first; when it is a string, a config key, a log message or a TODO, grep
is correct and the server has nothing to say.

The four that pay for themselves, each against the search you would otherwise
run:

| Question | `LSP` | why not grep |
|---|---|---|
| who calls this? | `findReferences` | grep finds the name; it misses aliased imports and re-exports, and it hits comments, strings and unrelated same-named symbols |
| where does this come from? | `goToDefinition` | the import path is often a barrel or a re-export, not the definition |
| what implements this? | `goToImplementation` | implementations rarely name the thing they implement |
| what does this file contain? | `documentSymbol` | reading 800 lines to find three methods |

`hover` answers "what type is this actually" for anything inferred or generic,
which is a question the source text cannot answer at all.

Two honest limits. **Dynamic dispatch is invisible to every language server** —
reflection, `send`, `getattr`, string-keyed DSLs — so in those languages grep
the name as well. And **no server, no answer**: if the language has none
installed, `LSP` returns nothing, and that is a reason to fall back to text
search and say so, never to report a caller list as complete.

### Writing against a library — the installed source before recall

The API you remember is the API of whatever version you were trained on, and
you have no error bar on that. A renamed method, an argument that moved, a
default that flipped — each one reads fine in review and fails at runtime.

Before writing a call into a library you do not have open:

1. **Check the version this repo pins**, in the lockfile rather than the
   manifest range. Documentation for 3.x answers nothing about the 2.x that is
   installed.
2. **Read the installed source.** It is already on disk — `site-packages`,
   `node_modules`, `vendor/`. `hover` and `goToDefinition` answer "what does
   this actually take" from the code that will run, which outranks every
   description of it.
3. **Only then go outward**, to the project's own documentation — or to a
   documentation tool if the session happens to have one.

A documentation server or CLI, where one is configured, is a faster step 3 than
a search engine. It does not replace steps 1 and 2: it answers from an index of
published docs, not from the version on this disk, and that index is a third
party's summary of a source you can read directly.

Two honest limits, the same shape as the language server's. **No source, no
claim** — when the library is not vendored and no docs tool is configured, name
the call you could not verify instead of writing it confidently. And **an index
can be right about the library and wrong about your version**: where it
disagrees with the installed source, the installed source wins, and the
disagreement is worth reporting rather than quietly resolving.

### Before you write a helper — the duplicate has a different name

The helper you are about to write may already exist, and the reason you are
about to write it is that you searched for *your* name for it and found
nothing. That silence is not evidence. The copy that ends up in the repo is
never named the same as the original — if it were, you would have found it.

So before writing a new function, in this order:

1. **Read the export list of the module you are already importing from.** Not
   your memory of it, and not the part of it your current import line names.
2. **Search by shape, not by name**, where the session has something that can —
   a code index that searches by similarity is exactly this, and it takes the
   body you are about to write rather than a guess at what it was called.
3. **Only then write it.**

Two outcomes are not "write your own", and both get mistaken for it:

- **It exists but is not exported.** One word is missing. Report it as a
  pending splice — name the file, the symbol, and that it needs exporting.
- **It exists in a file you do not own.** The rule against editing another
  agent's file and the rule against duplicating do not conflict here. Report
  it; the coordinator applies the one-word change.

Reporting the splice costs one round trip. It is the cheapest instruction in
this file, and copying instead is among the most expensive: the copies drift
apart, and by the time anyone counts them there are several that no longer
agree.

**A copy is not a deferral.** `ponytail:` records work you chose not to do, not
a rule you decided to break — and a duplicate filed as a future cleanup, in the
vocabulary of good practice, is harder to catch than one filed as nothing at
all. If you copied anyway, say you copied.

### Changing a signature
- Before changing a public signature, resolve the caller set with `LSP`
  (`findReferences`, plus `goToImplementation` for an interface member).
- Dynamic dispatch hides call sites from every language server — grep the name
  too where the language allows reflection, `send`, `getattr`, or DSL callbacks.
- Every caller and implementor either satisfies the new signature or is updated
  in the same change.
- No language server for that language? Fall back to text search and say in
  your report that the caller list is unverified.

### Running a command that can ask you a question
- A CLI that prompts has no answer in a non-interactive session: it waits, and
  the task ends when the turn does. `printf '\n' |` does not rescue it — a
  prompt reads the terminal, not stdin.
- Look for the non-interactive flag first (`--yes`, `--non-interactive`, an env
  var). If there is none, drive it with `expect`, and say in your report which
  prompt you answered and how. The next person meets the same one.
- A prompt asking for a password, a token, or permission to destroy something
  is a stop, not a puzzle to automate. Report it and let a person run it.

### Reporting done
- "Done" is a claim, and a claim needs the command and its result.
- Report: files touched, the verify command + its actual output, anything
  deliberately left undone and why.
- Never report green on a suite you did not run.
- **Write the report in English**, whatever language the task arrived in. It
  is read by another agent, next to your charter and these rules, and those are
  English. Whoever is talking to the user answers them in their language; that
  is a different channel — and so is the tree. What you commit follows the
  repository's language, under *Natural language*.
- **Report the fields, not the story.** Whoever dispatched you re-derives the
  diff itself, so your account of how you got there is read once and thrown
  away — it is the one part worth cutting to nothing.
- The cutting stops at the evidence. The verify command and its **actual
  output** stay verbatim, because a summarised error is not a result, and
  anything you left undone keeps the reason you left it.
