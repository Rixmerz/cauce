---
name: swift-rules
description: Swift conventions — optionals discipline, value types, error handling, structured concurrency. Use ONLY when the file under edit or review is Swift (.swift); do NOT apply to any other language.
---

# Swift Rules

> Apply ONLY when the file under edit or review is Swift (`.swift`). If the
> current file is not Swift, do not use this skill — it does not apply to other
> languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Prefer `let` over `var`; prefer `struct`/`enum` (value types) over `class`
- Unwrap optionals with `if let`/`guard let`/`??` — use guard for early exit
- Model states with `enum` + associated values; switch exhaustively
- Use `throws`/`do-catch` with typed `Error` values; propagate with `try`
- Use `async`/`await` and structured concurrency (`Task`, `async let`, actors)
- Use `[weak self]` in escaping closures that could form retain cycles
- Mark classes `final` unless designed for inheritance
- Name for clarity at the call site (Swift API Design Guidelines)
- Use `Codable` for serialization; `Result` only where a callback demands it
- Before changing a `public`/`open` signature, run `findReferences` on it; for a protocol requirement run `goToImplementation` to get the conforming types. Every caller and conformance in that list compiles against the new signature or is updated in this change

## DON'T
- Don't force-unwrap (`!`) or force-`try!`/`as!` unless provably safe
- Don't use implicitly unwrapped optionals (`T!`) outside IBOutlets/init dance
- Don't retain `self` strongly in long-lived escaping closures
- Don't do blocking work on the main actor/thread
- Don't use reference types when a value type models the data correctly
- Don't ignore compiler warnings or leave `print()` diagnostics in shipping code
- Don't subclass `NSObject`/use `@objc` unless interop actually requires it

## Navigation — the language server, not grep

`LSP` (`goToImplementation`, `findReferences`, `goToDefinition`, `documentSymbol`)
resolves bindings; grep matches text. Two Swift-specific reasons that gap bites:

- **Protocol conformance is often declared in an extension in another file.**
  The type's own definition may say nothing about the protocol you are changing.
  `goToImplementation` finds the conformances; grep needs you to guess.
- **Protocol extensions supply default implementations.** A method a call site
  uses may exist in no conforming type at all.

`@objc`, `#selector` and `NSClassFromString` are beyond sourcekit-lsp. Grep
those too.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Bind parameters in every SQLite/Postgres driver call — never interpolate into SQL (CWE-89)
- Store credentials in the Keychain — never `UserDefaults`, a plist, or the app bundle (CWE-522)
- `SecRandomCopyBytes` or `SystemRandomNumberGenerator` for tokens (CWE-330)
- A successful `Codable` decode is not validation — range-check and constrain decoded values (CWE-20)
- Validate TLS trust properly rather than disabling ATS or accepting any certificate (CWE-295)
- Never build a file path from user input without containment checking it (CWE-22)
