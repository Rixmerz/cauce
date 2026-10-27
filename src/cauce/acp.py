"""`cauce acp`: cauce as an Agent Client Protocol agent over stdio, for an editor
that speaks ACP (Zed's `agent_servers`).

ACP v1 is JSON-RPC 2.0, one JSON message per line. stdout carries only protocol
messages; anything else goes to stderr. Each `session/prompt` is one `cauce run`
(or `cauce tasks` / `cauce stats` / `cauce cancel N` for `/tasks`, `/stats` and
`/cancel N`) in the session's folder, its progress shown as one tool call and its
report as the answer. A `session/cancel` ends the turn at once, and the task goes on
for `CANCEL_WINDOW_S`: an editor sends it before the next prompt of a thread, so a
prompt that follows keeps the task running, and only a cancel nothing follows
cancels it. It is a way in, never a power: no `--allow`, `--allow-approval` or
`--keep` is passed, and the `mcpServers` a client sends are ignored, since the
servers a worker gets come from the person's own registry only. Standard library
only.
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

from cauce import __version__, criteria

PROTOCOL_VERSION = 1
CANCEL_GRACE_S = 10.0  # after a cancel, how long the process has to end before its group is killed
CANCEL_WINDOW_S = 3.0  # after a session/cancel, how long a prompt may still follow it and keep the task running
TAIL_LINES = 40  # the stderr lines a tool call's content shows
PIPES_JOIN_S = 5.0  # a grandchild that kept a pipe open must not hold a finished prompt back
TASK_NUMBER = re.compile(r"#(\d+)")
TASK_ID = re.compile(r"#?(\d+)")
DIFF_EVERY_S = 10.0  # how often a running worker's files are read again for the editor's diffs

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
BUSY = -32000
SESSION_NOT_FOUND = -32002

COMMANDS = {
    "tasks": "what cauce has queued, running and stopped here (`cauce tasks`)",
    "stats": "where cauce's time and money go (`cauce stats`)",
    "cancel": "stop a task now, as `/cancel 7` (`cauce cancel 7`)",
}
READING_COMMANDS = ("tasks", "stats")
MCP_NOTICE = ("cauce ignored the `mcpServers` your editor sent: the servers a worker gets come from your own "
              "cauce registry only, never from a client.")
NO_LOAD_NOTICE = ("cauce keeps no saved threads, so this one cannot be restored: open a new thread and ask again "
                  "(`/tasks` lists the work cauce still has).")

# A handler that answered the request itself, or will when its work ends.
DEFERRED = object()


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(eq=False)
class Run:
    """One prompt's process. `task` is the first `#N` its stderr named. `released` is a turn a
    session/cancel ended: its prompt is answered and nothing more of it is said, while the process goes
    on until `decided` (a prompt followed, or the window ran out)."""

    proc: Any
    call_id: str
    request_id: Any
    task: int | None = None
    released: bool = False
    answered: bool = False
    decided: threading.Event = field(default_factory=threading.Event)
    exited: threading.Event = field(default_factory=threading.Event)
    closed: bool = False
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    diffs: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)


@dataclass(eq=False)
class Session:
    cwd: str
    mcp_ignored: bool
    run: Run | None = None  # set and checked on the reader's thread, cleared with the prompt's answer
    waiting: Run | None = None  # the run a cancel released, until a prompt keeps it or its window runs out


def command_argv(text: str) -> tuple[str, list[str]]:
    """The cauce command a prompt asks for: a slash command, else a request to run. Only the exact
    forms are commands (`/tasks now` is a request), but `/cancel` without a task's number is refused:
    run as a request it would start a worker on that word."""
    said = text.strip()
    name = said[1:] if said.startswith("/") else ""
    if name in READING_COMMANDS:
        return name, [name]
    words = name.split()
    if words[:1] == ["cancel"]:
        found = TASK_ID.fullmatch(words[1]) if len(words) == 2 else None
        if found is None:
            raise RpcError(INVALID_PARAMS, "/cancel takes a task's number, as `/cancel 7`")
        return "cancel", ["cancel", found.group(1)]
    return "run", ["run", text]


def kept_notice(task: int | None) -> str:
    """What a prompt that followed a cancel says of the task it left running."""
    if task is None:
        return "cauce: your earlier request keeps running; `/tasks` lists it.\n\n"
    return f"cauce: task #{task} keeps running; `/tasks` lists it and `/cancel {task}` stops it.\n\n"


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


def task_diffs(task_id: int, live: bool) -> list[dict[str, Any]]:
    """A task's files as ACP `diff` content, before and after, at their path in its checkout: while it
    runs, its worktree as it is now; once it ended, the branch it kept. Read only; [] when nothing
    can be read."""
    from pathlib import Path

    from cauce import progress
    from cauce.store import Store

    store = Store.open()
    try:
        task = store.get_task(task_id)
        started = store.last_event(task_id, "attempt_started")
        ended = None if live else store.final_event(task_id)
    finally:
        store.close()
    if task is None or not task.get("cwd"):
        return []
    if live:
        found = progress.texts(progress.worktree(task_id), ((started or {}).get("data") or {}).get("base"))
    else:
        data = (ended or {}).get("data") or {}
        if not data.get("branch"):
            return []
        found = progress.texts(Path(task["cwd"]), data.get("base"), data["branch"])
    return [{"type": "diff", "path": str(Path(task["cwd"]) / f["path"]), "oldText": f["old"],
             "newText": f["new"] or ""} for f in (found or {}).get("files", [])]


class Agent:
    def __init__(self, out: TextIO, err: TextIO, popen: Callable[..., Any], killpg: Callable[[int, int], None],
                 grace: float, diffs: Callable[[int, bool], list[dict[str, Any]]] = task_diffs,
                 diff_every: float = DIFF_EVERY_S) -> None:
        self._out, self._err = out, err
        self._popen, self._killpg, self._grace = popen, killpg, grace
        self._diffs, self._diff_every = diffs, diff_every
        self._write = threading.Lock()
        self._turns = threading.Lock()  # who answers a prompt, and what a cancel left waiting
        self._gone = False  # the client closed its end: nothing more can be said to it
        self._sessions: dict[str, Session] = {}
        self._threads: list[threading.Thread] = []
        self._methods: dict[str, Callable[[dict[str, Any], Any], Any]] = {
            "initialize": self._initialize,
            "authenticate": lambda params, request_id: {},
            "session/new": self._new_session,
            "session/load": self._load_session,
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
        # No prompt can follow a closed input: a cancel still waiting is the cancel it was.
        with self._turns:
            waiting = [s.waiting for s in self._sessions.values() if s.waiting is not None]
        for run in waiting:
            run.decided.set()
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

    def _load_session(self, params: dict[str, Any], request_id: Any) -> None:
        """cauce advertises `loadSession: false` and keeps no thread, but an editor restoring a saved
        one asks anyway: say what to do instead of "method not found"."""
        raise RpcError(SESSION_NOT_FOUND, NO_LOAD_NOTICE)

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
        command, tail = command_argv(text)
        try:
            proc = self._popen([sys.executable, "-m", "cauce", *tail], cwd=session.cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                               start_new_session=True)
        except OSError as exc:
            raise RpcError(INTERNAL_ERROR, f"could not start cauce in {session.cwd}: {exc}") from exc
        run = Run(proc=proc, call_id="cauce-" + uuid.uuid4().hex[:12], request_id=request_id)
        with self._turns:
            session.run = run
            earlier, session.waiting = session.waiting, None
        if earlier is not None:
            # A prompt after a cancel is the person's next request, not a stop: the task it left goes on.
            earlier.decided.set()
            if not earlier.exited.is_set():
                self.update(session_id, {"sessionUpdate": "agent_message_chunk",
                                         "content": {"type": "text", "text": kept_notice(earlier.task)}})
        self.update(session_id, {"sessionUpdate": "tool_call", "toolCallId": run.call_id,
                                 "title": f"cauce {command}", "kind": "execute", "status": "in_progress"})
        if command == "run" and (items := criteria.items(text)):
            # The request's own items are the plan the editor shows: a worker keeps no list of its own.
            run.plan = [{"content": item, "priority": "medium", "status": "pending"} for item in items]
            self.update(session_id, {"sessionUpdate": "plan", "entries": run.plan})
        thread = threading.Thread(target=self._finish, args=(session_id, session, run), name=f"acp-{run.call_id}")
        self._threads.append(thread)
        thread.start()
        if command == "run":
            threading.Thread(target=self._follow_diffs, args=(session_id, run), name=f"acp-diffs-{run.call_id}",
                             daemon=True).start()
        return DEFERRED

    def _cancel(self, params: dict[str, Any], request_id: Any) -> None:
        """The turn ends now, since an editor waits for its answer before it sends the next prompt, but
        the task does not: an editor cancels the turn it is leaving, so what the person asked for goes on
        unless nothing follows within `CANCEL_WINDOW_S` (`_after_cancel`), or `/cancel N` says so."""
        session = self._sessions.get(params.get("sessionId")) if isinstance(params.get("sessionId"), str) else None
        run = session.run if session is not None else None
        if session is None or run is None or not self._claim(session, run, released=True):
            return
        self.reply(run.request_id, {"stopReason": "cancelled"})
        thread = threading.Thread(target=self._after_cancel, args=(session, run), name=f"acp-cancel-{run.call_id}")
        self._threads.append(thread)
        thread.start()

    def _claim(self, session: Session, run: Run, *, released: bool = False) -> bool:
        """The right to answer the run's prompt, given once: its end and a cancel can arrive together."""
        with self._turns:
            if run.answered:
                return False
            run.answered, run.released = True, released
            session.run = None  # a prompt is refused while one runs: the session's is this run
            if released:
                session.waiting = run
            return True

    def _after_cancel(self, session: Session, run: Run) -> None:
        run.decided.wait(CANCEL_WINDOW_S)
        with self._turns:
            if session.waiting is not run:
                return  # a prompt followed: the task goes on
            session.waiting = None
        self._stop(session.cwd, run)

    # -- one prompt --------------------------------------------------------

    def _finish(self, session_id: str, session: Session, run: Run) -> None:
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
        if run.released:
            return  # a cancel ended its turn and answered it: nothing of this run is said now
        status = "completed" if code == 0 else "failed"
        self._show_end(session_id, run, status)
        self._report(session_id, session, "".join(stdout))
        if self._claim(session, run):
            self.reply(run.request_id, {"stopReason": "end_turn"})

    def _show_end(self, session_id: str, run: Run, status: str) -> None:
        final: dict[str, Any] = {"sessionUpdate": "tool_call_update", "toolCallId": run.call_id, "status": status}
        if run.task is not None:
            try:
                kept = self._diffs(run.task, False)
            except Exception:  # a view: what cannot be read is left out, never the answer
                traceback.print_exc(file=self._err)
                kept = []
            if kept or run.diffs:
                run.diffs = kept
                final["content"] = self._content(run)
        self.update(session_id, final)
        if run.plan and status == "completed":
            # A pass answered every item; anything short of one leaves them as they were.
            self.update(session_id, {"sessionUpdate": "plan",
                                     "entries": [{**e, "status": "completed"} for e in run.plan]})

    def _report(self, session_id: str, session: Session, text: str) -> None:
        if session.mcp_ignored:
            session.mcp_ignored = False
            text = MCP_NOTICE + ("\n\n" + text if text else "")
        if text:
            self.update(session_id, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": text}})

    def _follow_stderr(self, session_id: str, run: Run) -> None:
        """cauce prints its progress on stderr: each line is the tool call's title, with the last
        lines as its content."""
        for raw in run.proc.stderr:
            line = raw.rstrip()
            if not line or run.closed:
                continue
            if run.task is None and (found := TASK_NUMBER.search(line)):
                run.task = int(found.group(1))
                if run.plan:
                    run.plan = [{**e, "status": "in_progress"} for e in run.plan]
                    self._say(session_id, run, {"sessionUpdate": "plan", "entries": run.plan})
            run.tail.append(line)
            self._say(session_id, run, {"sessionUpdate": "tool_call_update", "toolCallId": run.call_id,
                                        "title": line, "content": self._content(run)})

    def _say(self, session_id: str, run: Run, update: dict[str, Any]) -> None:
        """An update of the run's turn, unless a cancel ended it: the client has had its answer, and the
        task goes on unseen. The pipes are still read, or the process would block on them."""
        if not run.released:
            self.update(session_id, update)

    @staticmethod
    def _content(run: Run) -> list[dict[str, Any]]:
        text = [{"type": "content", "content": {"type": "text", "text": "\n".join(run.tail)}}] if run.tail else []
        return text + run.diffs

    def _follow_diffs(self, session_id: str, run: Run) -> None:
        """While the run's worker writes, its files as diffs beside the progress lines: read from its
        worktree every `DIFF_EVERY_S`, sent only when they changed."""
        while not run.exited.wait(self._diff_every):
            if run.task is None or run.closed or run.released:
                continue
            try:
                found = self._diffs(run.task, True)
            except Exception:  # a view: what cannot be read is left out
                traceback.print_exc(file=self._err)
                continue
            if found != run.diffs and not run.closed:
                run.diffs = found
                self._say(session_id, run, {"sessionUpdate": "tool_call_update", "toolCallId": run.call_id,
                                            "content": self._content(run)})

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
          grace: float = CANCEL_GRACE_S, diffs: Callable[[int, bool], list[dict[str, Any]]] = task_diffs,
          diff_every: float = DIFF_EVERY_S) -> int:
    """Answer the client's messages until its input ends and the running prompts finish."""
    Agent(stdout, stderr or sys.stderr, popen, killpg, grace, diffs, diff_every).serve(stdin)
    return 0
