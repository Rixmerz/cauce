---
name: php-rules
description: PHP coding conventions — strict types, typed properties, PSR standards, safe SQL. Use ONLY when the file under edit or review is PHP (.php); do NOT apply to any other language.
---

# PHP Rules

> Apply ONLY when the file under edit or review is PHP (`.php`). If the current
> file is not PHP, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Start every file with `declare(strict_types=1);`
- Type every property, parameter, and return (including `void`/`never`/nullable `?T`)
- Use `===`/`!==` (strict comparison) — never `==`
- Use constructor property promotion and `readonly` for immutable data
- Use `enum` (PHP 8.1+) instead of class constants for closed sets
- Use prepared statements / parameter binding for all SQL — never interpolate input
- Follow PSR-12 formatting and PSR-4 autoloading
- Throw typed exceptions; catch specific types, add context
- Prefer `match` over `switch` for value mapping (strict, exhaustive)
- Use `??` and `?->` instead of `isset()` ladders
- Before changing a public function or method signature, run `findReferences` on it; for an interface method run `goToImplementation` to get the implementing classes. Every caller and implementor in that list satisfies the new signature or is updated in this change

## DON'T
- Don't suppress errors with `@`
- Don't use `extract()`, `eval()`, or variable variables (`$$x`)
- Don't build SQL/HTML/shell strings from user input without escaping
- Don't rely on loose truthiness for `0`/`""`/`"0"` — check explicitly
- Don't use globals or `static` mutable state for request data
- Don't mix business logic into templates
- Don't return mixed `false`-or-value sentinels — throw or return null with a nullable type
- Don't ignore `phpstan`/`psalm` findings without justification

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `documentSymbol`)
resolves bindings; grep matches text. Two PHP-specific reasons that gap bites:

- **Traits compose methods into classes.** The method a call site uses is
  defined in a trait, not in the class it is called on, so grep inside the class
  finds nothing.
- **Namespaced `use` aliases rename the symbol.** `use A\B as C` means the name
  at the call site is not the name at the declaration.

`__call`, `__get` and container-resolved services are invisible to intelephense.
Grep those too, and say the caller list is unverified.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Prepared statements with bound parameters for every query — the single most common finding in PHP code (CWE-89)
- `password_hash`/`password_verify` for passwords — never md5, sha1, or a hand-rolled salt (CWE-916, CWE-327)
- `random_bytes`/`random_int` for tokens (CWE-330), `hash_equals` for comparison (CWE-208)
- Escape output for its destination context — `htmlspecialchars` for HTML, never a raw echo of user data (CWE-79)
- Never `unserialize()` untrusted input; use `json_decode` (CWE-502)
- Never `include`/`require` a path derived from a request (CWE-98), and never `eval()` (CWE-95)
