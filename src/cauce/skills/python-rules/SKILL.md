---
name: python-rules
description: Python coding conventions — modern typing, async discipline, safe subprocess, tooling. Use ONLY when the file under edit or review is Python (.py/.pyi); do NOT apply to any other language.
---

# Python Rules

> Apply ONLY when the file under edit or review is Python (`.py`/`.pyi`). If the
> current file is not Python, do not use this skill — it does not apply to other
> languages.

Precedence: `engineering-baseline` settles conflicts — safety outranks
everything, and the project's existing conventions outrank every preference
stated below.

## DO
- Use type hints on all function signatures
- Use `dataclass(frozen=True, slots=True)` for value objects
- Validate at system boundaries with the project's validation layer; plain dataclasses internally
- Use `pathlib.Path` instead of `os.path`
- Use f-strings for string formatting
- Use `match`/`case` for structural pattern matching (3.10+)
- Use built-in generics (`list[str]`, not `List[str]`) (3.9+)
- Use `X | Y` union syntax (3.10+), not `Union[X, Y]`
- Prefer `asyncio.TaskGroup` (3.11+) for fan-out where any failure should cancel the rest. `gather(return_exceptions=True)` is still the right tool when you need every result including the failures — TaskGroup has no equivalent
- Use `functools.cache`/`lru_cache` for pure functions with repeated inputs
- Use `asyncio.to_thread()` for blocking calls in async code
- Specify `encoding="utf-8"` when opening files
- Use `subprocess.run(["cmd", "arg"], check=True)` (no `shell=True`)
- Add context to errors: `raise ValueError(f"Invalid user {user_id}") from e`
- Use `Enum` for finite option sets instead of magic strings
- Before changing a public signature, run `findReferences` on it. Every caller in that list satisfies the new signature or is updated in this change
- Dynamic dispatch (`getattr`, registries, plugin entry points) hides call sites no server can resolve — grep the name as well before a signature change lands

## DON'T
- Don't use `import *` in production code
- Don't catch bare `Exception` without re-raising or logging
- Don't use mutable default arguments (`def f(items=[])`)
- Don't use `type()` for type checks — use `isinstance()` or Protocol
- Don't use `os.system()` or `subprocess` with `shell=True`
- Don't expect `asyncio.to_thread()` to parallelise CPU-bound work — the GIL serialises it; use a process pool
- Don't mix sync and async without `to_thread()`
- Don't use strings for structured data (dates, money, IDs) — use proper types
- Don't create metaclasses when `__init_subclass__` or decorators suffice
- Don't log unstructured strings where the project has a structured logger — emit key/value fields

## Tooling — greenfield defaults only

These are recommendations for a project that has **not** already chosen. Rung 3
of the precedence rule outranks this section: a repo on pip, Flake8, and stdlib
`logging` stays there. Never migrate a project's toolchain as a side effect of
an unrelated change.

- New project: `uv` for dependency management, `ruff check` + `ruff format` and
  `mypy --strict` in CI (one tool instead of Flake8 + Black + isort)
- New project validating external input: `Pydantic` at the boundary
- New project needing machine-readable logs: `structlog` with JSON output

## Navigation — the language server, not grep

`LSP` (`findReferences`, `goToDefinition`, `goToImplementation`, `documentSymbol`)
resolves bindings; grep matches text. Three Python-specific reasons that gap bites:

- **`__init__.py` re-exports.** The module a caller imports from is usually not
  where the thing is defined. `goToDefinition` lands on the definition; grep on
  the import path lands on the re-export.
- **Decorators rename the callable.** A `@app.route`-style wrapper means the
  name at the call site and the name in the `def` need not match.
- **Method calls resolve by type.** `client.send(...)` tells grep nothing about
  which `send` — `goToDefinition` on the call site does.

`getattr`, `**kwargs` dispatch and string-keyed registries are invisible to
pyright. Grep those too, and say the caller list is unverified.

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Parameterize SQL with the driver's placeholders or bound params — never an f-string, `%`, or `.format()` into a query (CWE-89)
- `subprocess.run([...], shell=False)` with an argument list; never `shell=True`, never `os.system()` (CWE-78)
- Never `pickle.loads` or `yaml.load` untrusted data — `yaml.safe_load`, and a signed format instead of pickle (CWE-502)
- Use `secrets` (not `random`) for tokens (CWE-330), and `hmac.compare_digest` to compare them (CWE-208)
- Resolve a user-supplied path and check it stays inside the intended root before opening it (CWE-22)
- Disable entity resolution when parsing XML (`defusedxml`) (CWE-611)
