"""Servers a task needs running, started by locks around the run and stopped after it.

A worker is one `claude -p` process: a dev server it starts in its shell stops
when it finishes, and a task that needs the app up ("check the screen at
localhost:4200") had to wait for a person to start it first. With `--service
'npm run dev@4200'`, locks starts the command in the task's own directory (its
worktree, so the server serves the work), waits until the port listens, runs
the attempts, and stops the command's whole process group when the run ends.

A service is the person's flag, never a file's: a command a repository commits
is not one locks runs. It is a command and a port, not a shell line: no pipes,
no `&&`. A port already listening is taken as the service up, and left alone.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: How long a service has to start listening.
WAIT_S = 120.0
POLL_S = 0.5
#: How long a stopped service has to exit before it is killed.
STOP_S = 10.0
_SPEC = re.compile(r"^(?P<command>.+?)@(?P<port>\d{2,5})$")


class ServiceError(RuntimeError):
    """A service that could not start or never listened; the message says which and why."""


@dataclass(frozen=True)
class Service:
    command: str
    port: int

    @property
    def label(self) -> str:
        return f"`{self.command}` on localhost:{self.port}"


def parse(spec: str) -> Service:
    """`npm run dev@4200` → the command and the port it listens on."""
    match = _SPEC.match(spec.strip())
    if not match or not 0 < int(match.group("port")) < 65536 or not shlex.split(match.group("command")):
        raise ServiceError(f"{spec!r} is no service: give a command and the port it listens on, "
                           "like 'npm run dev@4200'")
    return Service(match.group("command").strip(), int(match.group("port")))


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _stop(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=STOP_S)


@contextlib.contextmanager
def running(services: Sequence[Service], cwd: Path, log: Path, env: Mapping[str, str] | None = None, *,
            wait_s: float = WAIT_S, sleep: Callable[[float], None] = time.sleep,
            popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> Iterator[list[str]]:
    """Start each service unless its port already listens, wait for it, yield what is
    up (one line each), and stop what was started, whatever happens in between."""
    started: list[subprocess.Popen] = []
    lines: list[str] = []
    try:
        for service in services:
            if listening(service.port):
                lines.append(f"{service.label}: already up, left as it is")
                continue
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as out:
                try:
                    proc = popen(shlex.split(service.command), cwd=str(cwd), env={**os.environ, **(env or {})},
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
                except OSError as exc:
                    raise ServiceError(f"{service.label} could not start: {exc}") from exc
            started.append(proc)
            deadline = time.monotonic() + wait_s
            while not listening(service.port):
                if proc.poll() is not None:
                    raise ServiceError(f"{service.label} exited ({proc.returncode}) before it listened; "
                                       f"its output is in {log}")
                if time.monotonic() >= deadline:
                    raise ServiceError(f"{service.label} did not listen within {int(wait_s)}s; its output is in {log}")
                sleep(POLL_S)
            lines.append(f"{service.label}: started by locks for this task")
        yield lines
    finally:
        for proc in reversed(started):
            _stop(proc)
