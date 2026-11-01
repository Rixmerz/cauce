---
name: typescript-rules
description: TypeScript coding conventions — strict types, discriminated unions, boundary validation. Use ONLY when the file under edit or review is TypeScript/JavaScript (.ts/.tsx/.js/.jsx); do NOT apply to any other language.
---

# TypeScript Rules

> Apply ONLY when the file under edit or review is TypeScript/JavaScript
> (`.ts`/`.tsx`/`.js`/`.jsx`). If the current file is not TS/JS, do not use this
> skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Use `strict: true` in tsconfig
- Prefer `satisfies` over `as` for type validation without widening
- Use discriminated unions for state modeling (status field + data per variant)
- Use `as const` for literal arrays that define union types
- Prefer generics over overloads when possible
- Use branded types for domain IDs (`UserId`, `OrderId`) to prevent mixing
- Use exhaustive checking with `assertNever` in switch defaults
- Model expected (non-exceptional) failures as values rather than throws, and be consistent per module — a single `Result` island in a throwing codebase is worse than either convention alone
- Use `using`/`await using` for resource cleanup (TS 5.2+)
- Validate at system boundaries (user input, external APIs) with the project's schema library
- Before changing an exported signature, run `findReferences` on it; for an interface or abstract member run `goToImplementation`. Structural typing means anything with a matching shape conforms, so check re-exported and type-only call sites in that list too. Every entry type-checks against the new signature or is updated in this change

## DON'T
- Don't use `any` — use `unknown` and narrow
- Don't use `enum` — use `as const` objects or union types
- Don't use `namespace` — use ES modules
- Don't use legacy decorators (`experimentalDecorators`) — use Stage 3 (TS 5.0+)
- Don't create wrapper types for primitives without branded types
- Don't use `!` (non-null assertion) except in tests
- Don't ignore TypeScript errors with `@ts-ignore` — use `@ts-expect-error` if unavoidable
- Don't take a green test as proof the module loads. A failed ESM import breaks
  the whole module, not the one call, and a test that mocks that import never
  loads the real one — so it passes on a module nothing can import. Check it:
  `node -e "import('./path.js').then(m => console.log(Object.keys(m)))"`
- Don't reach for `--forceExit` to get a clean run. It kills the process before
  resources close, hiding the leaked timer or unclosed connection a route test
  exists to surface. A suite that won't exit is a finding — run it with
  `--detectOpenHandles` and report what it names.

## Tooling — greenfield defaults only

Recommendations for a project that has **not** already chosen; the project's
incumbent wins. Never migrate a toolchain as a side effect of another change.

- `strict: true` plus `noUncheckedIndexedAccess` in a new tsconfig
- A schema validator (Zod/Valibot) at the boundaries, not hand-written guards
- `Result`-style error values only where adopted module-wide

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `goToImplementation`, `documentSymbol`)
resolves bindings; grep matches text. Three TypeScript-specific reasons that gap
bites:

- **Barrel files.** `export * from './x'` means the path an import names is
  almost never the definition site. `goToDefinition` goes through the barrel.
- **Aliased and type-only imports.** `import { a as b }` and
  `import type { T }` both defeat a grep for the original name.
- **Overloads share one name.** `findReferences` on the signature you are
  changing separates the callers that bind to it from the ones that do not.

Types are erased at runtime, so anything reached through `any`, a string index
or a dynamic `import()` is beyond the server. Grep those too.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Parameterize SQL with the driver's bound params — never a template literal into a query (CWE-89)
- Never `eval` or `new Function` on external input (CWE-94); use `execFile` with an argument array instead of `child_process.exec` (CWE-78)
- Treat every external payload as `unknown` and narrow it through a schema before use (CWE-20)
- Use `crypto.randomUUID`/`randomBytes` for tokens (CWE-330) and `timingSafeEqual` to compare secrets (CWE-208)
- Never interpolate user data into `innerHTML`, `dangerouslySetInnerHTML`, or a `v-html` binding (CWE-79)
- Never fetch a URL built from user input server-side without an allowlist (CWE-918)
