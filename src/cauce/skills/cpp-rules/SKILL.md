---
name: cpp-rules
description: C and C++ conventions — RAII, ownership, no raw new/delete, bounds and lifetime safety. Use ONLY when the file under edit or review is C/C++ (.c/.h/.cpp/.cc/.cxx/.hpp/.hxx/.C/.H); do NOT apply to any other language.
---

# C / C++ Rules

> Apply ONLY when the file under edit or review is C or C++
> (`.c`/`.h`/`.cpp`/`.cc`/`.cxx`/`.hpp`/`.hxx`/`.C`/`.H`). If the current file is
> neither, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO (C++)
- RAII for every resource; wrap ownership in `unique_ptr`/`shared_ptr`, never raw owning pointers
- Prefer stack objects and value semantics; pass big objects by `const&`
- Use `std::` containers/algorithms over hand-rolled loops and C arrays
- Mark single-arg constructors `explicit`; mark overrides `override`; mark leaf classes `final`
- Follow the Rule of Zero (or Five if you manage a resource by hand)
- Use `enum class`, `constexpr`, `nullptr`, and `[[nodiscard]]` where they add safety
- Use `std::span`/`std::string_view` for non-owning views (C++20/17)
- Check every allocation / fallible call; propagate errors explicitly
- Before changing a declaration in a header, run `findReferences` on it; for a virtual member run `goToImplementation` to get every override. Every caller and override in that list compiles against the new signature or is updated in this change

## DO (C)
- Check the return of `malloc`/`realloc`/`fopen` etc.; free exactly once
- Pair every allocation with a single owner responsible for freeing it
- Use `sizeof(*ptr)` in allocations; bound every buffer write (`snprintf`, not `sprintf`)

## DON'T
- Don't use `new`/`delete`/`malloc`/`free` directly in modern C++ — use smart pointers/containers
- Don't return references/pointers to locals; don't use after free/move
- Don't use `strcpy`/`strcat`/`gets`/`sprintf` — use bounded variants
- Don't rely on implementation-defined or undefined behavior (signed overflow, aliasing)
- Don't cast away `const`; don't C-cast in C++ — use `static_cast`/`reinterpret_cast`
- Don't ignore compiler warnings — build with `-Wall -Wextra` and a sanitizer in CI

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `documentSymbol`, `hover`)
resolves bindings; grep matches text. Three C/C++-specific reasons that gap
bites:

- **Declaration and definition are in different files.** Grep for a name returns
  the header and the translation unit and cannot tell you which one a caller
  binds to.
- **Overloads share a name.** `findReferences` on the specific declaration you
  are changing returns only the calls that resolve to it.
- **The preprocessor rewrites the code before the compiler sees it.** A name
  produced by a macro is not in any file grep will search.

`hover` resolves `auto` and template parameters to concrete types, which the
source does not state.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Bound every buffer write (`snprintf`, `std::string`, `std::span`) — `strcpy`/`strcat`/`sprintf`/`gets` are findings, not style notes (CWE-787, CWE-121)
- Check for integer overflow before any size computation that feeds an allocation or an index (CWE-190, CWE-680)
- Never pass a user-controlled string as a format string (CWE-134)
- Never use a pointer after free or after move (CWE-416); never return a reference to a local (CWE-562)
- Zero secrets before freeing them (`explicit_bzero`/`SecureZeroMemory`) — a plain `memset` can be optimized away (CWE-244)
- Build with a sanitizer (ASan/UBSan) in CI; an unsanitized C++ suite proves very little about memory safety
