---
name: go-rules
description: Go coding conventions — context discipline, error wrapping, goroutine safety, small interfaces. Use ONLY when the file under edit or review is Go (.go); do NOT apply to any other language.
---

# Go Rules

> Apply ONLY when the file under edit or review is Go (`.go`). If the current
> file is not Go, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- `context.Context` always first parameter
- Add context to errors: `fmt.Errorf("doing X: %w", err)`
- Use `errors.Is()` for sentinels, `errors.As()` for custom types
- `defer cancel()` immediately after creating a context
- `defer mu.Unlock()` immediately after `mu.Lock()`
- Close channels only from sender, exactly once
- Every blocking goroutine must listen to `ctx.Done()`
- Use table-driven tests with `t.Run()`
- Define interfaces at consumer, not provider
- Keep interfaces small (1-3 methods)
- Use functional options for optional configuration
- Design structs so zero value is useful
- Use `internal/` for private packages
- Profile before optimizing (`pprof`)
- Before changing an exported signature, run `findReferences` on it; for an interface method run `goToImplementation` to get the implementing types. Every caller and implementor in that list satisfies the new signature or is updated in this change

## DON'T
- Don't store `context.Context` in a struct
- Don't `panic` in library code — return errors
- Don't ignore errors silently
- Don't use `init()` unless absolutely necessary
- Don't use generic package names (`util`, `helpers`, `common`)
- Don't force OOP patterns (deep embedding, getters/setters)
- Don't create interfaces before concrete types exist
- Don't start goroutines inside functions without making concurrency explicit to caller

## Tooling — greenfield defaults only

Recommendations for a project that has **not** already chosen; the project's
incumbent router, logger, and test helpers win. Never swap one as a side effect
of another change.

- Routing: `net/http` alone covers most needs since the 1.22 pattern syntax;
  reach for `chi` when you want middleware composition. `gorilla/mux` is
  maintained again and is a fine incumbent — do not migrate off it for style
- Logging: `log/slog` (stdlib) for new code; `zerolog` when allocation-free
  logging is measured to matter
- `go vet` plus `staticcheck` in CI

## Navigation — the language server, not grep

`LSP` (`goToImplementation`, `findReferences`, `goToDefinition`, `documentSymbol`)
resolves bindings; grep matches text. Two Go-specific reasons that gap bites:

- **Interface satisfaction is implicit.** Nothing in a Go source file says
  "implements". `goToImplementation` on an interface method is the *only*
  reliable way to find its implementors — grep on the method name returns every
  unrelated type that happens to have one.
- **Embedded structs promote methods.** A method a type appears not to have may
  be promoted from an embedded field, so grep inside the type's own file finds
  nothing while the call site is perfectly valid.

Anything reached through `reflect` or a `map[string]func` registry is beyond
gopls. Grep those too.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Parameterize with `db.Query(q, args...)` — never `fmt.Sprintf` into SQL (CWE-89)
- `exec.Command("cmd", args...)` with separate arguments, never a shell string (CWE-78)
- Use `crypto/rand` for tokens (CWE-330) and `subtle.ConstantTimeCompare` for secret comparison (CWE-208)
- Render HTML with `html/template`, never `text/template` (CWE-79)
- Check `filepath.Clean` results against the intended root before opening a user-supplied path (CWE-22)
- Bound request bodies and concurrent work — an unbounded `io.ReadAll` on a request is a DoS (CWE-400)
