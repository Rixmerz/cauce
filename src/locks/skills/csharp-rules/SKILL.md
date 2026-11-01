---
name: csharp-rules
description: C# conventions — nullable reference types, async/await discipline, IDisposable, LINQ hygiene. Use ONLY when the file under edit or review is C# (.cs); do NOT apply to any other language.
---

# C# Rules

> Apply ONLY when the file under edit or review is C# (`.cs`). If the current
> file is not C#, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Enable nullable reference types (`#nullable enable`) and honor the annotations
- `async`/`await` all the way; return `Task`/`Task<T>`; suffix async methods with `Async`
- Pass and observe `CancellationToken` on async APIs
- Use `ConfigureAwait(false)` in library code
- `using` declarations for `IDisposable`; implement `IAsyncDisposable` for async cleanup
- Prefer `record`/`readonly struct` for immutable data
- Use expression-bodied members and pattern matching where they clarify
- Use `IEnumerable<T>`/`IReadOnlyList<T>` at boundaries; materialize with `ToList()` once
- Throw specific exceptions; use `ArgumentNullException.ThrowIfNull(x)`
- Before changing a public member's signature, run `findReferences` on it; for an interface member run `goToImplementation`. Overloads and implementing types are what a name grep gets wrong. Every entry in that list satisfies the new signature or is updated in this change

## DON'T
- Don't use `async void` except for event handlers
- Don't block on async (`.Result`, `.Wait()`, `.GetAwaiter().GetResult()`) — deadlock risk
- Don't ignore the nullable warnings with `!` unless provably safe
- Don't expose mutable collections as public fields — use properties/read-only views
- Don't catch `Exception` broadly or swallow it silently
- Don't enumerate an `IEnumerable` multiple times when it may be a deferred/expensive query
- Don't use `string` concatenation in loops — use `StringBuilder`/`string.Join`
- Don't leave `Console.WriteLine` for diagnostics — use `ILogger`

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `goToImplementation`, `documentSymbol`)
resolves bindings; grep matches text. Two C#-specific reasons that gap bites:

- **Partial classes split a type across files.** `documentSymbol` on one file
  shows you a fraction of the type; the members you are looking for may be in a
  generated partial you would never open.
- **Explicit interface implementations do not carry the plain name.**
  `void IFoo.Bar()` is invisible to a grep for `Bar(` at the call sites that use
  it through the interface.

Reflection and DI-container registration are invisible to the server. Grep those
too.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Parameterized `SqlCommand`, or EF Core's interpolation-safe `FromSqlInterpolated` — never a concatenated query (CWE-89)
- Never `BinaryFormatter`, and never a deserializer with unrestricted type handling on untrusted input (CWE-502)
- `Process` with `ArgumentList`, never a joined command string (CWE-78)
- `RandomNumberGenerator` for tokens (CWE-330), `CryptographicOperations.FixedTimeEquals` for comparison (CWE-208)
- Set `XmlResolver = null` on `XmlReaderSettings` (CWE-611)
- Validate a user-supplied path with `Path.GetFullPath` against the intended root (CWE-22)
