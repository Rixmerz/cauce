---
name: java-rules
description: Java coding conventions — immutability, Optional discipline, resource safety, modern language features. Use ONLY when the file under edit or review is Java (.java); do NOT apply to any other language.
---

# Java Rules

> Apply ONLY when the file under edit or review is Java (`.java`). If the current
> file is not Java, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Prefer `final` fields and immutable objects; use `record` for data carriers
- Use `Optional<T>` for return values that may be absent — never for fields or params
- `try-with-resources` for anything `AutoCloseable`; never manual `close()` in `finally`
- Program to interfaces (`List`, `Map`), instantiate concretes (`ArrayList`, `HashMap`)
- Use `java.time` (`Instant`, `LocalDate`) — never `Date`/`Calendar`
- Use `var` for local inference only when the type is obvious from the right side
- Validate arguments early: `Objects.requireNonNull(x, "x")`
- Prefer `enum` over int/string constants
- Use `equals`/`hashCode` together, or a `record`; keep them consistent
- Streams for transformation pipelines; plain loops when clearer or hot-path
- Catch the most specific exception; wrap with context via a cause
- Before changing a `public`/`protected` signature, run `findReferences` on it; for an interface or abstract method run `goToImplementation`. Overloads and implementing classes are what a method-name grep gets wrong. Every entry in that list compiles against the new signature or is updated in this change

## DON'T
- Don't return `null` collections — return `List.of()` / `Collections.emptyList()`
- Don't catch `Exception`/`Throwable` broadly or swallow with an empty block
- Don't use raw types (`List` instead of `List<String>`)
- Don't do field injection when constructor injection is possible
- Don't use `synchronized` on a public object you don't own; prefer `java.util.concurrent`
- Don't concatenate strings in loops — use `StringBuilder`
- Don't call overridable methods from a constructor
- Don't use checked exceptions for control flow
- Don't leave `System.out.println` in production code — use a logger (SLF4J)

## Navigation — the language server, not grep

`LSP` (`goToImplementation`, `findReferences`, `goToDefinition`, `documentSymbol`)
resolves bindings; grep matches text. Two Java-specific reasons that gap bites:

- **Overrides are indistinguishable by name.** Grep for `process(` returns every
  unrelated `process` in the repo; `findReferences` on the declaration returns
  the callers that actually bind to it, and `goToImplementation` returns the
  overrides.
- **The declaration and the callers are in different packages by design.**
  Interface-first code puts the thing you are changing far from everything that
  uses it.

Reflection, `ServiceLoader`, and annotation-driven wiring are invisible to
jdtls. Grep the name too, and say the caller list is unverified.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- `PreparedStatement` with placeholders — never string-concatenated SQL, and never a concatenated JPQL/HQL query (CWE-89)
- Never deserialize untrusted data with `ObjectInputStream` (CWE-502)
- `ProcessBuilder` with an argument list, never a joined shell string (CWE-78)
- `SecureRandom` for tokens (CWE-330), `MessageDigest.isEqual` for comparison (CWE-208), and a real KDF — bcrypt/scrypt/Argon2 — for passwords (CWE-916)
- Disable external entities on every XML parser and document builder (CWE-611)
- Validate a user-supplied path against its intended root before `Files.newInputStream` (CWE-22)
