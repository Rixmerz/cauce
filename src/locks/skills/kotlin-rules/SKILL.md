---
name: kotlin-rules
description: Kotlin conventions — null safety, immutability, coroutines discipline, idiomatic scope functions. Use ONLY when the file under edit or review is Kotlin (.kt/.kts); do NOT apply to any other language.
---

# Kotlin Rules

> Apply ONLY when the file under edit or review is Kotlin (`.kt`/`.kts`). If the
> current file is not Kotlin, do not use this skill — it does not apply to other
> languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Prefer `val` over `var`; prefer immutable collections (`listOf`, `mapOf`)
- Use nullable types deliberately; handle with `?.`, `?:`, `let` — not `!!`
- Model closed sets with `sealed class`/`enum`; use `when` exhaustively (no `else`)
- Use `data class` for value objects; `object` for singletons
- Structured concurrency: launch coroutines in a scope, respect cancellation, use `suspend`
- Use `Dispatchers.IO`/`Default` appropriately; never block a coroutine thread
- Use extension functions to keep call sites readable, sparingly
- Use scope functions with intent: `let` (nullable), `apply` (config), `run`/`with` (compute)
- Return `Result<T>` or throw specific exceptions — be consistent per module
- Before changing a public signature, run `findReferences` on it; for a `sealed`/interface member run `goToImplementation` to get the subtypes. Default arguments make call sites vary in arity, so read each one: every caller and implementor either satisfies the new signature or is updated in this change

## DON'T
- Don't use `!!` to silence nullability — restructure or check
- Don't use `lateinit` where a nullable or constructor value fits
- Don't run blocking I/O inside a coroutine without a proper dispatcher
- Don't leak `GlobalScope` coroutines — tie them to a lifecycle scope
- Don't overuse companion objects as dumping grounds for statics
- Don't write Java-style getters/setters — use properties
- Don't ignore platform types from Java interop — annotate/assert nullability at the boundary

## Navigation — the language server, not grep

`LSP` (`goToImplementation`, `findReferences`, `goToDefinition`, `documentSymbol`)
resolves bindings; grep matches text. Two Kotlin-specific reasons that gap bites:

- **Extension functions are declared away from the type they extend.** A call
  that reads like a member may be defined in an unrelated file, which is exactly
  the case grep cannot follow from the call site.
- **Overrides across a hierarchy share a name.** `goToImplementation` separates
  the real overrides from every same-named function in the repo.

Reflection and annotation-driven wiring are invisible to the server. Grep those
too.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- `PreparedStatement` or the ORM's bound parameters — never a string-interpolated query (CWE-89)
- Never deserialize untrusted data with Java serialization (CWE-502)
- `ProcessBuilder` with an argument list, never a joined shell string (CWE-78)
- `SecureRandom` for tokens (CWE-330), `MessageDigest.isEqual` for comparison (CWE-208)
- Disable external entities on every XML parser (CWE-611)
- Platform types from Java interop are unvalidated input at the boundary — annotate and check them (CWE-20)
