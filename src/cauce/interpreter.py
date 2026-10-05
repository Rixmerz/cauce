"""Which Python runs cauce.

`__main__` imports this before it checks the version, so it must run on any
python3: no syntax or import newer than 3.7 belongs here.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping

# Newest first. A bare `python3` older than 3.11 is common (macOS ships 3.9),
# and a newer one is often installed beside it under its own name.
NEWER_PYTHONS = ("python3.14", "python3.13", "python3.12", "python3.11")
OLD_ENOUGH = (3, 11)


def newer_python(env: Mapping[str, str], which: Callable[..., str | None] | None = None) -> str | None:
    """A Python 3.11+ to run cauce with, or None. Only names that carry their
    version are tried, so finding one costs no subprocess."""
    which = which or shutil.which
    if env.get("CAUCE_PYTHON") or env.get("CAUCE_REEXEC") == "1":
        return None  # a person chose the interpreter, or this already is the re-run
    for name in NEWER_PYTHONS:
        found = which(name, path=env.get("PATH"))
        if found:
            return found
    return None
