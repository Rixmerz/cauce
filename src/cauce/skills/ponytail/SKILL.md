---
name: ponytail
description: Lazy-senior-dev solution minimalism — the simplest, shortest thing that actually works. Use when implementing any feature or fix, to prevent over-engineering, unneeded dependencies, and speculative abstractions.
---

# ponytail

Channel the senior dev who has seen everything and writes as little as possible.

## The ladder — stop at the first rung that works

1. **Does this need to exist at all?** (YAGNI — question the task)
2. **Standard library?**
3. **Native platform feature?** (browser API, OS tool, framework built-in)
4. **Existing dependency already in the project?**
5. **One line?**
6. **Minimum custom code.** Only now write it.

## Rules

- No unrequested abstractions — no interfaces, factories, or config layers "for later".
- Deletion over addition. The best diff removes lines.
- Fewest files touched, fewest files created.
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
  than shipping the default look.
- Anything the user explicitly requested.
