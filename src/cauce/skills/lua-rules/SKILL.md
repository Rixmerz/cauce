---
name: lua-rules
description: Lua conventions — locals by default, explicit nil handling, 1-based indexing, careful metatables. Use ONLY when the file under edit or review is Lua (.lua); do NOT apply to any other language.
---

# Lua Rules

> Apply ONLY when the file under edit or review is Lua (`.lua`). If the current
> file is not Lua, do not use this skill — it does not apply to other languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Declare every variable `local` — never rely on globals implicitly
- Return a module table at the end of a file; keep helpers local
- Remember tables/arrays are 1-based; use `#t` only on sequences without holes
- Handle fallible calls with `pcall`/`xpcall`; return `nil, err` for recoverable errors
- Check for `nil` explicitly — only `nil` and `false` are falsy (`0` and `""` are truthy)
- Prefer `ipairs` for arrays, `pairs` for maps; don't mutate a table while iterating it
- Localize hot globals (`local insert = table.insert`) in performance-critical loops
- Set string patterns carefully — Lua patterns are not regex
- Before changing the signature of a function exposed on a module table, run `findReferences` on it, then grep the name as well — dynamic `require` and table indexing hide call sites from any server. Every caller found matches the new arity or is updated in this change

## DON'T
- Don't create accidental globals by forgetting `local`
- Don't assume `#t` is defined when the table has `nil` gaps
- Don't overuse metatables/`__index` magic where a plain function is clearer
- Don't use `==` across different types expecting coercion — Lua does not coerce in `==`
- Don't rely on table iteration order from `pairs`
- Don't `error()` with a bare string across module boundaries without context
- Don't leave `print` debugging in committed code — use the host's logger

## Navigation — the language server, not grep

`LSP` (`goToDefinition`, `findReferences`, `documentSymbol`) resolves bindings;
grep matches text. In Lua the honest guidance runs both ways:

- **Use it for ordinary module functions.** `require`-returned tables and
  `M.fn = function()` definitions resolve, and that is most of a normal file.
- **Do not trust it alone.** Everything is a table at runtime: methods assigned
  through a variable, `setmetatable` dispatch, and string-keyed lookups are
  invisible to the server.

So: language server first, grep second, and say the caller list is unverified
whenever the call goes through a table built at runtime.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Bind or escape SQL parameters through the driver — never `..` concatenation into a query (CWE-89)
- Never `load`/`loadstring` untrusted input (CWE-94), and never `os.execute`/`io.popen` with interpolation (CWE-78)
- In OpenResty, never store per-request data in a module-level global — it leaks across requests and users (CWE-488)
- Use the host's CSPRNG for tokens; `math.random` is not one (CWE-330)
- Escape output for its context before rendering it into HTML (CWE-79)
- Lua patterns are not regex — do not rely on one as a security filter (CWE-20)
