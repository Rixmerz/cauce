"""A module loaded on its first attribute read (`importlib.util.LazyLoader`): the same module
object every importer gets, so a test that patches an attribute patches it for everyone.

A command pays for what it imports before it does anything: the run loop (`orchestrate`, with
`launch` and `isolate` under it) is most of that, and the commands a session's mod runs every
few seconds (`board`, `endings`, `tasks`) never touch it. Standard library only.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType


def module(name: str) -> ModuleType:
    """`name` as it is in `sys.modules`, else loaded on its first attribute read."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.find_spec(name)
    if spec is None or spec.loader is None:
        return importlib.import_module(name)
    spec.loader = importlib.util.LazyLoader(spec.loader)
    found = importlib.util.module_from_spec(spec)
    sys.modules[name] = found
    spec.loader.exec_module(found)
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, found)
    return found
