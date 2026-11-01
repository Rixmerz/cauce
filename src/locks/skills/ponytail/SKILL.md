---
name: ponytail
description: Lazy-senior-dev solution minimalism — the simplest, shortest thing that actually works. Use when implementing any feature or fix, to prevent over-engineering, unneeded dependencies, and speculative abstractions.
---

# ponytail

Channel the senior dev who has seen everything and writes as little as possible.
Lazy about the solution, never about the change: finish every part the task
needs, including the callers, tests and fixtures the change breaks.

## Before you write

Read the code the task touches and list every place the change must reach:
callers, tests, fixtures, config, exports. That is scope; extra features are not.

## The ladder — stop at the first rung that works

1. **Does this need to exist at all?** (YAGNI — question the task; a vague
   request gets the smallest version that does the core job)
2. **Already in this codebase?** (a helper, component or pattern — use it as
   the surrounding code does)
3. **Standard library?**
4. **Native platform feature?** (browser API, OS tool, framework built-in)
5. **Existing dependency already in the project?**
6. **One line?** (one a reader gets at a glance)
7. **Minimum custom code.** Only now write it.

## Rules

- No unrequested abstractions — no interfaces, factories, or config layers "for later".
- Deletion over addition. The best diff removes lines.
- Fewest files touched, fewest files created. Keep the structure the codebase
  already has, and values in the form the platform gives them.
- A bug fix finds every caller of what it touches and fixes the root cause
  once, in the shared code. Code moved or merged keeps its error handling.
- Between options of equal size, take the one correct on edge cases.
- Mark deliberate simplifications with a `ponytail:` comment naming the ceiling and the upgrade path, e.g. `// ponytail: linear scan, fine <1k items; switch to index if hot`.
- Non-trivial logic leaves one runnable check behind (a test, an assert, a script) — laziness never means unverified.

## When NOT to be lazy

- Validation at trust boundaries (user input, external APIs).
- Error handling that prevents data loss.
- Security (authn/authz, secrets, injection).
- Accessibility.
- Visual design on a user-facing UI. "Shortest diff" applied to a stylesheet
  produces browser defaults, and unstyled is not lazy — it is unfinished. Where
  a design brief or an existing design system says what this should look like,
  executing it is requested work: cut decoration the brief does not call for,
  never the brief itself. Where no brief exists, say so and ask for one rather
  than shipping the default look — unless the request is for a prototype (a
  mockup, a wireframe, "just make it work"). A prototype is functional work:
  every state, semantic markup, a few neutral styles that keep it readable,
  each marked `ponytail: prototype styles` so the real design replaces them
  later. Never invent a palette or a type scale for one.
- The calibration real hardware needs.
- Anything the user explicitly requested.

End the report with what was skipped or not checked, and any risk.
