---
name: rust-rules
description: Rust coding conventions — error handling, ownership idioms, unsafe discipline, cargo tooling. Use ONLY when the file under edit or review is Rust (.rs); do NOT apply to any other language.
---

# Rust Rules

> Apply ONLY when the file under edit or review is Rust (`.rs`). If the current
> file is not Rust, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Use `?` for error propagation
- Give libraries a concrete error enum and applications a boxed/erased error
- Use `expect("reason")` over `unwrap()` when panic is intentional
- Add `// SAFETY:` comment to every `unsafe` block
- Use iterators (`.iter().map().filter().collect()`) over manual index loops
- Use enums for state modeling with exhaustive `match`
- Use the Newtype pattern for type safety (`struct UserId(u64)`)
- Use Typestate pattern when invalid states should not compile
- Derive `Debug`, `Clone`, `PartialEq` where applicable
- Use `&str` in function params, return `String` when owned
- Use `Cow<str>` when data frequently passes through unmodified
- Put logic in `lib.rs`, keep `main.rs` as thin wrapper
- Use `[workspace.dependencies]` for shared dependency versions
- Run `cargo fmt`, `cargo clippy -D warnings`, `cargo audit` in CI
- Document public APIs with `# Examples`, `# Errors`, `# Panics`
- Use `lto = true` and `codegen-units = 1` in release profile
- Before changing a `pub` signature, run `findReferences` on it; for a trait method run `goToImplementation` to get every implementor. Each caller and implementor in that list compiles against the new signature or is updated in this change

## DON'T
- Don't use `unwrap()` in production — lacks context
- Don't use `.clone()` reflexively to silence borrow checker — restructure code
- Don't write `unsafe` without documented justification
- Don't use multiple lifetimes on structs when owning data (`String` vs `&str`) works
- Don't ignore compiler warnings or clippy lints
- Don't use `String` params when `&str` suffices (forces caller allocation)
- Don't write C-style index loops — iterators are idiomatic and faster
- Don't use `async-std` — obsolete (unmaintained since early 2024), use Tokio or smol
- Don't use `Box<dyn FnMut>` callbacks for observer — use channels
- Don't add `#[inline]` without profiling data
- Don't ignore `cargo audit` results in CI

## Tooling — greenfield defaults only

These are recommendations for a project that has **not** already chosen. Never
migrate a project's toolchain as a side effect of an unrelated change.

- `thiserror` for a library's error enum, `anyhow` for an application's erased error

## Navigation — the language server, not grep

`LSP` (`goToImplementation`, `findReferences`, `goToDefinition`, `hover`)
resolves bindings; grep matches text. Three Rust-specific reasons that gap bites:

- **Trait impls live anywhere.** `impl Trait for Type` can sit in any module.
  `goToImplementation` finds them; grep needs you to guess the file.
- **Blanket impls exist in no file you would think to search.**
  `impl<T: Bound> Trait for T` gives a type an implementation nobody wrote for
  it.
- **Macros generate the code you are looking for.** Grep sees the macro
  invocation; `goToDefinition` resolves through the expansion.

`hover` is worth more in Rust than almost anywhere else: it prints the inferred
type and the resolved lifetimes, neither of which appears in the source.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Use bound parameters (`sqlx::query!`, `.bind(..)`) — never `format!` into SQL (CWE-89)
- `Command::new(..).arg(..)` with separate arguments, never `sh -c` with interpolation (CWE-78)
- Use `OsRng`/`getrandom` for tokens (CWE-330) and a constant-time compare (`subtle`) for secrets (CWE-208)
- Every `unsafe` block is a security surface — an undocumented one is a finding, not a style note (CWE-787, CWE-416)
- Prefer checked arithmetic (`checked_add`, `try_into`) wherever the value derives from input; release builds wrap silently (CWE-190)
- Keep `cargo audit`/`cargo deny` in CI and treat an advisory as a finding, not a warning
