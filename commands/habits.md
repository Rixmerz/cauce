---
description: Review the habits cauce found — edits followed by the same check, again and again — and install the ones worth it in this repository as hooks, after you pick them
---

A habit is a sequence the sessions repeat: an edit of a file type, then the
same command (a type check, a linter, a formatter, the tests of what changed).
Installed, it is a `PostToolUse` hook in this repository's
`.claude/settings.local.json`: after each edit of that type it runs the
command, and when the command fails the session reads its output at once,
instead of spending a turn running it. Your job: find which candidates are
worth that here, show them to the person, and install the ones they pick.

When `cauce` alone is not found (exit 127), it is
`${CLAUDE_PLUGIN_ROOT}/bin/cauce`; call it by that path.

## 1. Read what there is

```bash
cauce habits list --json      # candidates that passed the gates, best first
cauce habits status --json    # installed habits: where, runs, failures, on or off
```

The candidates come from every repository's sessions; `status` says where each
installed one lives (`repo`). The repository here is the top of the checkout
of the current directory (`git rev-parse --show-toplevel`).

## 2. Decide, for this repository

A candidate is worth installing here only when all of these hold:

- **The file type is this repository's.** Its first step names it
  (`edit:.ts`, `write:.py`); files of that type are tracked here.
- **The last step is a check you can name here.** A signature keeps the
  program, never its arguments: `bash:npm-run`, `bash:npx-jest`. Find the
  command this repository would run for it, from its own files —
  `package.json` scripts, `pyproject.toml`, `Makefile`, the lock file for the
  package manager (`pnpm exec`, `npx`, `uv run`). Never guess one it does not
  have.
- **It is fast and touches nothing.** It runs after every edit and the
  session waits for it (110 seconds at most). A type check, a linter, a
  formatter, or the tests of what changed (`--onlyChanged`,
  `--findRelatedTests`) are; the whole suite, a build, an install, a deploy,
  anything that writes outside the working tree or talks to the network are
  not. A formatter rewrites the file the session just edited: say so.
- **It is not here already.** The same command on the same file type in
  `status` for this repository is installed; `cauce habits install` says so
  and installs nothing, but do not offer it.
- **It passes now.** Run the command once. A check that fails before any edit
  would fail after each one and turn itself off after three: tell the person
  what failed instead of offering it.

A candidate whose last step is the model running a script (`bash:python3`,
`bash:node` on a one-off file) or a tool you cannot place is no habit to
install, whatever its count: leave it out and say why in one line.

Also look at what is installed here: a habit that is **off** (three failures
in a row) or a **second copy** of one already on (same command, same file
type) is one to remove. Offer that too.

## 3. Ask, then install

Show the person one list: each habit you propose, as *after editing `.ts`,
run `npx tsc --noEmit`* with its count (×N in M sessions), and each one to
remove with why. Ask which to apply with `AskUserQuestion` (multiSelect),
one option per habit, up to four per question. Without that tool, ask in
your reply and wait.

Install only what the person picked — a hook runs a shell command after
every edit, and that is their call, never yours:

```bash
cauce habits install <candidate id> --command "<command>" --repo <top of the checkout>
cauce habits uninstall <habit id>
```

Then say what is installed and where, and that `cauce habits uninstall <id>`
removes any of them. A session that does not run a new hook yet picks it up
when it starts again.

## Never

- Install a habit the person did not pick, or a command they did not see.
- Install in a repository other than this one: run the skill there.
- Edit `.claude/settings.local.json` by hand; `cauce habits install` and
  `uninstall` keep it and cauce's record in step.
