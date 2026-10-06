"""The `--allow` rules that would let a refused tool call through.

A refusal is recorded as the call itself: `Bash(cd app; tg -n "x|y" src; npx tsc
--noEmit 2>&1 | tail -15)`. Allowing that string allows that exact string, and
cut short for a report it allows nothing. What a person grants is a rule per
program, so each refusal is turned into those:

- a shell command is split into the commands it chains (`;`, `&&`, `||`, `|`),
  each becoming a prefix rule for its program, with the subcommand where the
  program is a runner (`Bash(npm run build:*)`, `Bash(npx tsc:*)`,
  `Bash(git status:*)`). Claude Code checks every part of a chain, so every
  part needs its rule. `bash -c '…'` is read for the script inside it;
- an absolute file path is written `//path`: a single leading slash is read as
  relative to the settings file, and the rule would match nothing;
- a URL becomes its domain.

Commands that change nothing a rule guards (`cd`, `echo`, `true`) are left out.
These are suggestions printed for a person to pass; nothing here grants them.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

FILE_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n"})
_HARMLESS = frozenset({"cd", "echo", "true", "false", "exit", "set", ":", "printf"})
#: Programs whose next word is what a rule should name: `npm run build`, not `npm`.
_RUNNERS = {"npm": 2, "pnpm": 2, "yarn": 2, "bun": 2, "npx": 1, "git": 1, "cargo": 1, "go": 1, "uv": 1,
            "source": 1, ".": 1, "docker": 1, "kubectl": 1, "make": 1, "poetry": 1, "pip": 1}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RULE = re.compile(r"^(?P<tool>[A-Za-z_][\w-]*)\((?P<target>.*)\)$", re.DOTALL)


def rules(tool_name: str, tool_input: dict[str, Any] | None) -> list[str]:
    """The rules for one refused call, as its tool and input arrived."""
    given = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash" and given.get("command"):
        return [f"Bash({prefix}:*)" for prefix in commands(str(given["command"]))] or ["Bash"]
    if tool_name in FILE_TOOLS and (given.get("file_path") or given.get("notebook_path")):
        path = str(given.get("file_path") or given.get("notebook_path"))
        return [f"{tool_name}(/{path})" if path.startswith("/") and not path.startswith("//") else
                f"{tool_name}({path})"]
    if tool_name == "WebFetch" and given.get("url"):
        host = urlparse(str(given["url"])).hostname
        return [f"WebFetch(domain:{host})"] if host else ["WebFetch"]
    return [tool_name]


def from_refusals(refused: Iterable[str]) -> tuple[str, ...]:
    """Rules read back from refusals already written as `Tool(target)`, for a
    task recorded before the rules were. A target cut at its end loses the
    commands chained after the cut, never the ones before it."""
    out: list[str] = []
    for text in refused:
        found = _RULE.match(text.strip())
        if found is None:
            out.append(text.strip())
            continue
        tool, target = found["tool"], found["target"]
        if tool == "Bash" and target.endswith("..."):
            # The last command was cut mid-word: what is left of it names nothing.
            out += [f"Bash({prefix}:*)" for prefix in commands(target.removesuffix("..."))[:-1]]
            continue
        key = {"Bash": "command", "WebFetch": "url"}.get(tool, "file_path")
        out += rules(tool, {key: target})
    return tuple(dict.fromkeys(r for r in out if r))


def commands(command: str) -> list[str]:
    """The program prefix of every command a shell line chains, harmless ones aside."""
    if "<<" in command:
        command = command.split("\n", 1)[0]  # a heredoc's body is data, not commands
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    out: list[str] = []
    part: list[str] = []
    for token in [*tokens, ";"]:
        if token in _SEPARATORS:
            out += _prefix(part)
            part = []
        else:
            part.append(token)
    return list(dict.fromkeys(out))


def _prefix(words: list[str]) -> list[str]:
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words or words[0] in _HARMLESS or words[0].startswith(("(", "$", "`", "<", ">")):
        return []
    program = words[0]
    if program in ("bash", "sh", "zsh") and len(words) > 2 and words[1] == "-c":
        return commands(words[2])
    if program in ("python", "python3") and len(words) > 2 and words[1] == "-m":
        return [" ".join(words[:3])]
    following: list[str] = []
    for word in words[1:1 + _RUNNERS.get(program, 0)]:
        if word.startswith(("-", ">", "<", "2>", "\"", "'")):
            break  # an option may take a value: past it, a word is not the subcommand
        following.append(word)
    return [" ".join([program, *following])]
