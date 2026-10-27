"""`cauce acp`: cauce as an Agent Client Protocol agent over stdio, for an editor
that speaks ACP (Zed's `agent_servers`).

ACP v1 is JSON-RPC 2.0, one JSON message per line. stdout carries only protocol
messages; anything else goes to stderr. Each `session/prompt` is one `cauce run`
(or `cauce tasks` / `cauce stats` for `/tasks` and `/stats`) in the session's
folder, its progress shown as one tool call and its report as the answer. It is
a way in, never a power: no `--allow`, `--allow-approval` or `--keep` is passed,
and the `mcpServers` a client sends are ignored, since the servers a worker gets
come from the person's own registry only. Standard library only.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TextIO

from cauce import __version__

PROTOCOL_VERSION = 1
CANCEL_GRACE_S = 10.0  # after a cancel, how long the process has to end before its group is killed
TAIL_LINES = 40  # the stderr lines a tool call's content shows
PIPES_JOIN_S = 5.0  # a grandchild that kept a pipe open must not hold a finished prompt back
TASK_NUMBER = re.compile(r"#(\d+)")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
BUSY = -32000

COMMANDS = {
    "tasks": "what cauce has queued, running and stopped here (`cauce tasks`)",
    "stats": "where cauce's time and money go (`cauce stats`)",
}
MCP_NOTICE = ("cauce ignored the `mcpServers` your editor sent: the servers a worker gets come from your own "
              "cauce registry only, never from a client.")

# A handler that answered the request itself, or will when its work ends.
DEFERRED = object()


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(eq=False)
class Run:
    """One prompt's process. `task` is the first `#N` its stderr named."""

    proc: Any
    call_id: str
    task: int | None = None
    cancelled: bool = False
    exited: threading.Event = field(default_factory=threading.Event)
    closed: bool = False


@dataclass(eq=False)
class Session:
    cwd: str
    mcp_ignored: bool
    run: Run | None = None  # set and checked on the reader's thread, cleared by the prompt's before it answers


def prompt_text(blocks: list[Any]) -> str:
    """The text of the text blocks, one per line, and a resource link's uri."""
    lines = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            lines.append(block["text"])
        elif block.get("type") == "resource_link" and isinstance(block.get("uri"), str):
            lines.append(block["uri"])
    return "\n".join(lines)


class Agent:
    def __init__(self, out: TextIO, err: TextIO, popen: Callable[..., Any], killpg: Callable[[int, int], None],
                 grace: float) -> None:
        self._out, self._err = out, err
        self._popen, self._killpg, self._grace = popen, killpg, grace
        self._write = threading.Lock()
        self._gone = False  # the client closed its end: nothing more can be said to it
        self._sessions: dict[str, Session] = {}
        self._threads: list[threading.Thread] = []
        self._methods: dict[str, Callable[[dict[str, Any], Any], Any]] = {
            "initialize": self._initialize,
            "authenticate": lambda params, request_id: {},
            "session/new": self._new_session,
            "session/prompt": self._prompt,
            "session/cancel": self._cancel,
        }

    # -- the wire ----------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        # json.dumps escapes every newline, so a message is always one line.
        line = json.dumps({"jsonrpc": "2.0", **message}) + "\n"
        with self._write:
            if self._gone:
                return
            try:
                self._out.write(line)
                self._out.flush()
            except (BrokenPipeError, ValueError):
                self._gone = True

    def reply(self, request_id: Any, result: Any) -> None:
        self.send({"id": request_id, "result": result})

    def fail(self, request_id: Any, code: int, message: str) -> None:
        self.send({"id": request_id, "error": {"code": code, "message": message}})

    def update(self, session_id: str, update: dict[str, Any]) -> None:
        self.send({"method": "session/update", "params": {"sessionId": session_id, "update": update}})

    # -- the loop ----------------------------------------------------------

    def serve(self, lines: Iterable[str]) -> None:
        for line in lines:
            if line.strip():
                self._handle(line)
        for thread in self._threads:
            thread.join()

    def _handle(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self.fail(None, PARSE_ERROR, "Parse error")
            return
        if not isinstance(message, dict):
            self.fail(None, INVALID_REQUEST, "Invalid Request")
            return
        method = message.get("method")
        if not isinstance(method, str):
            if "result" not in message and "error" not in message:
                self.fail(message.get("id"), INVALID_REQUEST, "Invalid Request")
            return  # otherwise a client's answer: cauce asks the client nothing
        is_request = "id" in message
        handler = self._methods.get(method)
        if handler is None:
            if is_request:
                self.fail(message["id"], METHOD_NOT_FOUND, "Method not found")
            return
        params = message.get("params")
        try:
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            result = handler(params, message["id"] if is_request else None)
        except RpcError as exc:
            if is_request:
                self.fail(message["id"], exc.code, exc.message)
            return
        except Exception:  # the protocol's boundary: say it on stderr, answer, keep serving
            traceback.print_exc(file=self._err)
            if is_request:
                self.fail(message["id"], INTERNAL_ERROR, "Internal error")
            return
        if is_request and result is not DEFERRED:
            self.reply(message["id"], result)

    # -- methods -----------------------------------------------------------

    def _initialize(self, params: dict[str, Any], request_id: Any) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "agentInfo": {"name": "cauce", "title": "cauce", "version": __version__},
            "authMethods": [],
        }

    def _new_session(self, params: dict[str, Any], request_id: Any) -> object:
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise RpcError(INVALID_PARAMS, "cwd must be an absolute path")
        session_id = "sess_" + uuid.uuid4().hex[:16]
        self._sessions[session_id] = Session(cwd=cwd, mcp_ignored=bool(params.get("mcpServers")))
        # The answer first: the client does not know the session before it.
        self.reply(request_id, {"sessionId": session_id})
        self.update(session_id, {"sessionUpdate": "available_commands_update", "availableCommands": [
            {"name": name, "description": description} for name, description in COMMANDS.items()]})
        return DEFERRED

    def _session(self, params: dict[str, Any]) -> tuple[str, Session]:
        session_id = params.get("sessionId")
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise RpcError(INVALID_PARAMS, f"unknown session {session_id!r}")
        return session_id, session

    def _prompt(self, params: dict[str, Any], request_id: Any) -> object:
        if request_id is None:
            raise RpcError(INVALID_REQUEST, "session/prompt is a request")
        session_id, session = self._session(params)
        blocks = params.get("prompt")
        if not isinstance(blocks, list):
            raise RpcError(INVALID_PARAMS, "prompt must be a list of content blocks")
        text = prompt_text(blocks)
        if not text.strip():
            raise RpcError(INVALID_PARAMS, "the prompt has no text")
        if session.run is not None:
            raise RpcError(BUSY, "a prompt is already running in this session")
        slash = text.strip()[1:] if text.strip().startswith("/") else None
        command, tail = (slash, [slash]) if slash in COMMANDS else ("run", ["run", text])
        try:
            proc = self._popen([sys.executable, "-m", "cauce", *tail], cwd=session.cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                               start_new_session=True)
        except OSError as exc:
            raise RpcError(INTERNAL_ERROR, f"could not start cauce in {session.cwd}: {exc}") from exc
        run = Run(proc=proc, call_id="cauce-" + uuid.uuid4().hex[:12])
        session.run = run
        self.update(session_id, {"sessionUpdate": "tool_call", "toolCallId": run.call_id,
                                 "title": f"cauce {command}", "kind": "execute", "status": "in_progress"})
        thread = threading.Thread(target=self._finish, args=(session_id, session, run, request_id),
                                  name=f"acp-{run.call_id}")
        self._threads.append(thread)
        thread.start()
        return DEFERRED

    def _cancel(self, params: dict[str, Any], request_id: Any) -> None:
        session = self._sessions.get(params.get("sessionId")) if isinstance(params.get("sessionId"), str) else None
        run = session.run if session is not None else None
        if run is None or run.cancelled:
            return
        run.cancelled = True
        threading.Thread(target=self._stop, args=(session.cwd, run), name=f"acp-cancel-{run.call_id}",
                         daemon=True).start()

    # -- one prompt --------------------------------------------------------

    def _finish(self, session_id: str, session: Session, run: Run, request_id: Any) -> None:
        stdout: list[str] = []
        readers = [
            threading.Thread(target=lambda: stdout.extend(run.proc.stdout), daemon=True),
            threading.Thread(target=self._follow_stderr, args=(session_id, run), daemon=True),
        ]
        for reader in readers:
            reader.start()
        code = run.proc.wait()
        run.exited.set()
        for reader in readers:
            reader.join(PIPES_JOIN_S)
        run.closed = True
        status = "completed" if code == 0 and not run.cancelled else "failed"
        self.update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": run.call_id, "status": status})
        text = "".join(stdout)
        if session.mcp_ignored:
            session.mcp_ignored = False
            text = MCP_NOTICE + ("\n\n" + text if text else "")
        if text:
            self.update(session_id, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": text}})
        session.run = None
        self.reply(request_id, {"stopReason": "cancelled" if run.cancelled else "end_turn"})

    def _follow_stderr(self, session_id: str, run: Run) -> None:
        """cauce prints its progress on stderr: each line is the tool call's title, with the last
        lines as its content."""
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        for raw in run.proc.stderr:
            line = raw.rstrip()
            if not line or run.closed:
                continue
            if run.task is None and (found := TASK_NUMBER.search(line)):
                run.task = int(found.group(1))
            tail.append(line)
            self.update(session_id, {
                "sessionUpdate": "tool_call_update", "toolCallId": run.call_id, "title": line,
                "content": [{"type": "content", "content": {"type": "text", "text": "\n".join(tail)}}]})

    def _stop(self, cwd: str, run: Run) -> None:
        """Ask cauce to cancel the task the run named, and kill the process group when the run has not
        ended within the grace."""
        deadline = time.monotonic() + self._grace
        asked = False
        while not run.exited.is_set() and time.monotonic() < deadline:
            if not asked and run.task is not None:
                asked = True
                self._cancel_task(cwd, run.task)
            run.exited.wait(0.05)
        if not run.exited.is_set():
            # Gone between the check and the kill: nothing left to stop.
            with contextlib.suppress(ProcessLookupError):
                self._killpg(run.proc.pid, signal.SIGKILL)

    def _cancel_task(self, cwd: str, task: int) -> None:
        try:
            proc = self._popen([sys.executable, "-m", "cauce", "cancel", str(task)], cwd=cwd,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            proc.wait(timeout=self._grace)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"cauce acp: `cauce cancel {task}` did not finish: {exc}", file=self._err)


def serve(stdin: Iterable[str], stdout: TextIO, stderr: TextIO | None = None, *,
          popen: Callable[..., Any] = subprocess.Popen, killpg: Callable[[int, int], None] = os.killpg,
          grace: float = CANCEL_GRACE_S) -> int:
    """Answer the client's messages until its input ends and the running prompts finish."""
    Agent(stdout, stderr or sys.stderr, popen, killpg, grace).serve(stdin)
    return 0
