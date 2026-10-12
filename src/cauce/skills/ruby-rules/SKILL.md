---
name: ruby-rules
description: Ruby coding conventions — idiomatic blocks, safe navigation, frozen strings, small methods. Use ONLY when the file under edit or review is Ruby (.rb/.rake/.gemspec); do NOT apply to any other language.
---

# Ruby Rules

> Apply ONLY when the file under edit or review is Ruby (`.rb`/`.rake`/`.gemspec`).
> If the current file is not Ruby, do not use this skill — it does not apply to
> other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Add `# frozen_string_literal: true` at the top of every file
- Prefer guard clauses (`return unless x`) over nested conditionals
- Use `&.` (safe navigation) instead of manual nil checks
- Use keyword arguments for methods with 2+ params or any boolean flag
- Prefer `each`/`map`/`select`/`reduce` over manual index loops
- Use `attr_reader`/`attr_accessor` instead of hand-written accessors
- Raise specific `StandardError` subclasses, not bare `RuntimeError`/strings
- Use `fetch` for required hash keys so missing keys fail loudly
- Keep methods short; extract private helpers freely
- Follow standard naming: `snake_case` methods/vars, `CamelCase` classes, `?`/`!` suffixes
- Before changing a public method signature, run `findReferences` on it, then grep the name too — `send`, `method_missing`, and DSL callbacks are call sites no server resolves. Every caller found satisfies the new signature or is updated in this change

## DON'T
- Don't rescue `Exception` — rescue `StandardError` or a specific subclass
- Don't use `and`/`or` for boolean logic (precedence traps) — use `&&`/`||`
- Don't mutate a method argument the caller still owns
- Don't monkey-patch core classes in library/app code
- Don't use `for` loops — use iterators
- Don't leave `puts`/`p` debugging in committed code — use a logger
- Don't overuse metaprogramming when a plain method is clearer
- Don't ignore `rubocop` offenses without an inline disable + reason

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `documentSymbol`) resolves bindings;
grep matches text. In Ruby the honest guidance runs both ways:

- **Use it for ordinary definitions.** Modules included into a class put methods
  far from the class body, and `goToDefinition` follows that where grep cannot.
- **Do not trust it alone.** `define_method`, `method_missing`, `send`, and
  every DSL that builds methods at load time are invisible to ruby-lsp. In Ruby
  more than any other language here, a `findReferences` result is a floor on the
  caller set, not the caller set.

So: language server first, grep second, and say in your report that the caller
list is unverified whenever metaprogramming is in play.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Parameterize (`where("x = ?", v)`, `where(x: v)`) — never interpolate into a query string, `order`, or `pluck` (CWE-89)
- Never `system`/backticks/`eval` with interpolated input — pass an argument array to `system`/`Open3` (CWE-78, CWE-95)
- `YAML.safe_load`, and never `Marshal.load` on untrusted data (CWE-502)
- `SecureRandom` for tokens (CWE-330), `Rack::Utils.secure_compare` for comparison (CWE-208), bcrypt for passwords (CWE-916)
- Never `send`/`public_send`/`constantize` with a user-supplied name (CWE-470)
- Use strong parameters — a permissive `permit!` is mass assignment (CWE-915)
