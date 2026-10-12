# Skills cauce ships

These are the skills cauce's bundled roles carry (`../roles/`). Their text
goes into a worker's system prompt (`skills`), or the worker is told where
they are and reads them when they apply (`on_demand`). A skill of the same
name in `<cauce home>/skills/<name>/SKILL.md` takes the place of one of these.

None of them was written for cauce. Each keeps its authors' copyright and
license; where cauce changed the text, the change is listed.

## From vise

- **Source:** [Rixmerz/vise](https://github.com/Rixmerz/vise),
  `skills/<name>/SKILL.md` at commit `29b2537`.
- **License:** MIT, Copyright (c) 2026 Rixmerz. The full text is in
  [`LICENSE-vise`](LICENSE-vise).
- **Skills:** `engineering-baseline`, `ponytail`, `security-baseline`,
  `architecture`, `ui-critique`, `web-ui-rules`, `design-brief`, and the
  rules of fourteen languages (`bash-rules`, `cpp-rules`, `csharp-rules`,
  `go-rules`, `java-rules`, `kotlin-rules`, `lua-rules`, `php-rules`,
  `python-rules`, `ruby-rules`, `rust-rules`, `sql-rules`, `swift-rules`,
  `typescript-rules`).
- **What cauce changed:**
  - `engineering-baseline`: "every vise agent" became "every cauce role", and
    the mention of vise's `codelayer` became "a code index that searches by
    similarity".
  - `security-baseline`: the sentence on `.vise/quality.yaml` became "the one
    the repository already runs".
  - `design-brief`: the paragraphs on `vise shot` became tool-neutral advice
    to render and look, then measure.

vise's `ponytail` is its own shorter text of an idea that
[DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) (MIT)
made known as the lazy senior developer. That fuller plugin, with its review,
audit and debt skills, installs for Claude Code on its own, and a role can name
its skills (`ponytail:ponytail-review`) once it is installed.

## From Cloudflare

- **Source:** [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill),
  `skills/security-audit/` at commit `c1c8a8c`.
- **License:** MIT, Copyright (c) 2025-2026 Cloudflare, Inc. The full text is
  in [`security-audit/LICENSE`](security-audit/LICENSE).
- **Skill:** `security-audit`: `SKILL.md`, the companion guides, the report
  schema and the two validators, unchanged. The validators' own test files
  are left out.
- The `security-auditor` role carries it, in the skill's guidance mode unless
  a task asks for a full audit.
