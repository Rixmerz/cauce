from __future__ import annotations

import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import cauce
from cauce import acp

WAIT_S = 5.0
TASK_LINE = "cauce: #7 attempt 1 at opus/low"


class FakeProc:
    """A `cauce` process: its stderr lines, its stdout, and an exit that waits for `release()` when held."""

    def __init__(self, argv: list[str], kwargs: dict[str, Any], *, stderr: list[str] | None = None,
                 stdout: str = "", code: int = 0, hold: bool = False) -> None:
        self.argv, self.kwargs, self.pid, self.code = argv, kwargs, 4242, code
        self.stdout = io.StringIO(stdout)
        self._stderr = list(stderr or [])
        self._released = threading.Event()
        if not hold:
            self._released.set()

    @property
    def stderr(self) -> Iterator[str]:
        yield from (line + "\n" for line in self._stderr)
        # The pipe closes when the process ends.
        self._released.wait(WAIT_S)

    def release(self) -> None:
        self._released.set()

    def wait(self, timeout: float | None = None) -> int:
        if not self._released.wait(WAIT_S if timeout is None else timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.code


class Spawner:
    """The injected popen: the first call is the run, a `cancel` command is answered at once."""

    def __init__(self, release_on_cancel: bool = True, **run: Any) -> None:
        self.run, self.release_on_cancel = run, release_on_cancel
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.procs: list[FakeProc] = []
        self.killed: list[tuple[int, int]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        if argv[3] == "cancel":
            if self.release_on_cancel:
                self.procs[0].release()
            return FakeProc(argv, kwargs)
        proc = FakeProc(argv, kwargs, **self.run)
        self.procs.append(proc)
        return proc

    def killpg(self, pid: int, sig: int) -> None:
        self.killed.append((pid, sig))
        self.procs[0].release()


class Sink:
    """What the agent wrote to stdout, one JSON message per line."""

    def __init__(self) -> None:
        self._text = ""
        self._changed = threading.Condition()

    def write(self, text: str) -> int:
        with self._changed:
            self._text += text
            self._changed.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        with self._changed:
            text = self._text
        assert text == "" or text.endswith("\n")
        return [json.loads(line) for line in text.splitlines()]

    def wait_for(self, match) -> dict[str, Any]:
        found: list[dict[str, Any]] = []

        def seen() -> bool:
            found[:] = [m for m in self.messages() if match(m)]
            return bool(found)

        with self._changed:
            assert self._changed.wait_for(seen, WAIT_S), f"never written: {self.messages()}"
        return found[0]


class Client:
    def __init__(self, spawner: Spawner | None = None, **serve_args: Any) -> None:
        self.spawner = spawner or Spawner()
        self.out, self.err = Sink(), io.StringIO()
        self._lines: queue.Queue[str | None] = queue.Queue()
        serve_args.setdefault("grace", 30.0)
        serve_args.setdefault("diffs", lambda task, live: [])
        self.thread = threading.Thread(
            target=acp.serve, args=(iter(self._lines.get, None), self.out, self.err),
            kwargs={"popen": self.spawner, "killpg": self.spawner.killpg, **serve_args})
        self.thread.start()
        self._ids = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._ids += 1
        self.raw(json.dumps({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}}))
        return self._ids

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.raw(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def raw(self, line: str) -> None:
        self._lines.put(line)

    def answer(self, request_id: int) -> dict[str, Any]:
        return self.out.wait_for(lambda m: m.get("id") == request_id)

    def new_session(self, cwd: str = "/work/app", mcp: list[Any] | None = None) -> str:
        reply = self.answer(self.request("session/new", {"cwd": cwd, "mcpServers": mcp or []}))
        return reply["result"]["sessionId"]

    def prompt(self, session: str, *text: str) -> int:
        return self.request("session/prompt", {
            "sessionId": session, "prompt": [{"type": "text", "text": t} for t in text]})

    def updates(self, session: str) -> list[dict[str, Any]]:
        return [m["params"]["update"] for m in self.out.messages()
                if m.get("method") == "session/update" and m["params"]["sessionId"] == session]

    def close(self) -> None:
        self._lines.put(None)
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()


@pytest.fixture
def client() -> Iterator[Client]:
    made: list[Client] = []

    def make(spawner: Spawner | None = None, **serve_args: Any) -> Client:
        made.append(Client(spawner, **serve_args))
        return made[-1]

    yield make
    for c in made:
        for proc in c.spawner.procs:
            proc.release()
        c.close()


def test_initialize_says_what_cauce_is_and_can_do(client):
    c = client()
    reply = c.answer(c.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}))
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": False,
                              "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False}},
        "agentInfo": {"name": "cauce", "title": "cauce", "version": cauce.__version__},
        "authMethods": []}}
    assert c.answer(c.request("authenticate", {"methodId": "x"}))["result"] == {}


def test_a_new_session_is_answered_before_its_commands_are_announced(client):
    c = client()
    reply = c.answer(c.request("session/new", {"cwd": "/work/app", "mcpServers": []}))
    session = reply["result"]["sessionId"]
    commands = c.out.wait_for(lambda m: m.get("method") == "session/update")
    assert c.out.messages().index(reply) < c.out.messages().index(commands)
    assert commands["params"] == {"sessionId": session, "update": {
        "sessionUpdate": "available_commands_update",
        "availableCommands": [{"name": "tasks", "description": acp.COMMANDS["tasks"]},
                              {"name": "stats", "description": acp.COMMANDS["stats"]}]}}
    assert c.new_session() != session


def test_a_session_needs_an_absolute_folder(client):
    c = client()
    for params in ({}, {"cwd": "relative/dir"}, {"cwd": 5}):
        assert c.answer(c.request("session/new", params))["error"]["code"] == -32602


def test_a_prompt_is_one_cauce_run_shown_as_a_tool_call_and_answered_with_its_report(client):
    spawner = Spawner(stderr=[TASK_LINE, "cauce: #7 passed"], stdout="the report\n")
    c = client(spawner)
    session = c.new_session("/work/app")
    request = c.request("session/prompt", {"sessionId": session, "prompt": [
        {"type": "text", "text": "fix the login"},
        {"type": "resource_link", "uri": "file:///work/app/auth.py", "name": "auth.py"},
        {"type": "image", "data": "x", "mimeType": "image/png"},
        {"type": "text", "text": "and its tests"}]})
    reply = c.answer(request)
    assert reply["result"] == {"stopReason": "end_turn"}
    argv, kwargs = spawner.calls[0]
    assert argv == [sys.executable, "-m", "cauce", "run", "fix the login\nfile:///work/app/auth.py\nand its tests"]
    assert kwargs["cwd"] == "/work/app"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE

    shown = c.updates(session)[1:]  # the first announced the commands
    call_id = shown[0]["toolCallId"]
    assert shown == [
        {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": "cauce run", "kind": "execute",
         "status": "in_progress"},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": TASK_LINE,
         "content": [{"type": "content", "content": {"type": "text", "text": TASK_LINE}}]},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "title": "cauce: #7 passed",
         "content": [{"type": "content", "content": {"type": "text", "text": f"{TASK_LINE}\ncauce: #7 passed"}}]},
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "the report\n"}},
    ]
    # The answer to the prompt comes after everything it showed.
    assert c.out.messages()[-1] == reply


def test_the_request_s_items_are_the_plan_and_its_files_are_diffs(client):
    """The editor sees the request's items as the plan (in progress once the task started, completed
    on a pass) and the worker's files as diffs: its worktree while it runs, the kept branch after."""
    asked: list[tuple[int, bool]] = []
    live = {"type": "diff", "path": "/work/app/a.py", "oldText": None, "newText": "x = 1\n"}
    kept = {"type": "diff", "path": "/work/app/a.py", "oldText": None, "newText": "x = 2\n"}

    def diffs(task: int, running: bool) -> list[dict[str, Any]]:
        asked.append((task, running))
        return [live] if running else [kept]

    spawner = Spawner(stderr=[TASK_LINE], stdout="the report\n", hold=True)
    c = client(spawner, diffs=diffs, diff_every=0.01)
    session = c.new_session()
    request = c.prompt(session, "Add two things:\n- a sub function\n- a mul function")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("content", [None])[-1] == live)
    spawner.procs[0].release()
    c.answer(request)
    plans = [u["entries"] for u in c.updates(session) if u["sessionUpdate"] == "plan"]
    assert [[e["status"] for e in p] for p in plans] == [["pending"] * 2, ["in_progress"] * 2, ["completed"] * 2]
    assert [e["content"] for e in plans[0]] == ["a sub function", "a mul function"]
    final = next(u for u in c.updates(session) if u.get("status") == "completed")
    assert final["content"][-1] == kept and (7, True) in asked and (7, False) in asked


def test_a_run_short_of_a_pass_leaves_its_plan_unfinished(client):
    c = client(Spawner(stderr=[TASK_LINE], code=1))
    session = c.new_session()
    c.answer(c.prompt(session, "Do:\n- one\n- two"))
    plans = [u["entries"] for u in c.updates(session) if u["sessionUpdate"] == "plan"]
    assert plans[-1][0]["status"] == "in_progress"


def test_a_tool_call_shows_the_last_forty_stderr_lines(client):
    lines = [f"line {n}" for n in range(45)]
    c = client(Spawner(stderr=lines))
    session = c.new_session()
    c.answer(c.prompt(session, "go"))
    last = next(u for u in c.updates(session) if u.get("title") == "line 44")
    shown = last["content"][0]["content"]["text"].split("\n")
    assert shown == lines[5:]


def test_a_run_that_exits_nonzero_is_a_failed_tool_call_that_still_reports(client):
    c = client(Spawner(stderr=["cauce: #3 failed"], stdout="it failed\n", code=1))
    session = c.new_session()
    reply = c.answer(c.prompt(session, "go"))
    assert reply["result"] == {"stopReason": "end_turn"}
    updates = c.updates(session)
    assert [u["status"] for u in updates if "status" in u] == ["in_progress", "failed"]
    assert updates[-1] == {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "it failed\n"}}


@pytest.mark.parametrize("command", ["tasks", "stats"])
def test_slash_commands_run_the_reading_commands_instead(client, command):
    spawner = Spawner(stdout="the output\n")
    c = client(spawner)
    session = c.new_session()
    c.answer(c.prompt(session, f"/{command}"))
    assert spawner.calls[0][0] == [sys.executable, "-m", "cauce", command]
    updates = c.updates(session)
    assert updates[1]["title"] == f"cauce {command}"
    assert updates[-1]["content"]["text"] == "the output\n"


def test_a_text_that_only_starts_with_a_slash_command_is_a_request(client):
    spawner = Spawner()
    c = client(spawner)
    session = c.new_session()
    c.answer(c.prompt(session, "/tasks now"))
    assert spawner.calls[0][0][3:] == ["run", "/tasks now"]


def test_a_cancel_asks_cauce_to_cancel_the_task_the_run_named(client):
    spawner = Spawner(stderr=[TASK_LINE, "cauce: #8 other"], hold=True, code=-15)
    c = client(spawner)
    session = c.new_session("/work/app")
    request = c.prompt(session, "go")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("title") == TASK_LINE)
    c.notify("session/cancel", {"sessionId": session})
    reply = c.answer(request)
    assert reply["result"] == {"stopReason": "cancelled"}
    argv, kwargs = spawner.calls[1]
    assert argv == [sys.executable, "-m", "cauce", "cancel", "7"]
    assert kwargs["cwd"] == "/work/app"
    assert spawner.killed == []
    assert [u["status"] for u in c.updates(session) if "status" in u] == ["in_progress", "failed"]


def test_a_run_that_outlasts_its_cancel_is_killed_with_its_process_group(client):
    spawner = Spawner(release_on_cancel=False, stderr=[TASK_LINE], hold=True, code=-9)
    c = client(spawner, grace=0.2)
    session = c.new_session()
    request = c.prompt(session, "go")
    c.notify("session/cancel", {"sessionId": session})
    assert c.answer(request)["result"] == {"stopReason": "cancelled"}
    assert spawner.killed == [(4242, signal.SIGKILL)]


def test_a_second_prompt_while_one_runs_is_refused_and_the_first_goes_on(client):
    spawner = Spawner(hold=True, stdout="done\n")
    c = client(spawner)
    session = c.new_session()
    first = c.prompt(session, "one")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    second = c.prompt(session, "two")
    assert c.answer(second)["error"]["code"] == -32000
    assert len(spawner.calls) == 1
    spawner.procs[0].release()
    assert c.answer(first)["result"] == {"stopReason": "end_turn"}
    # The session is free again.
    spawner.run["hold"] = False
    other = c.prompt(session, "three")
    assert c.answer(other)["result"] == {"stopReason": "end_turn"}


def test_a_cancel_is_read_while_a_prompt_of_another_session_runs(client):
    spawner = Spawner(hold=True)
    c = client(spawner)
    busy, idle = c.new_session(), c.new_session()
    c.prompt(busy, "one")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    c.notify("session/cancel", {"sessionId": idle})  # nothing runs there: ignored
    assert c.answer(c.request("authenticate"))["result"] == {}


def test_a_client_mcp_list_is_ignored_and_said_once_in_the_first_answer(client):
    c = client(Spawner(stdout="the report\n"))
    session = c.new_session(mcp=[{"name": "files", "command": "x", "args": [], "env": []}])
    c.answer(c.prompt(session, "go"))
    c.answer(c.prompt(session, "again"))
    chunks = [u["content"]["text"] for u in c.updates(session) if u["sessionUpdate"] == "agent_message_chunk"]
    assert chunks == [acp.MCP_NOTICE + "\n\nthe report\n", "the report\n"]
    quiet = c.new_session()
    c.answer(c.prompt(quiet, "go"))
    assert [u["content"]["text"] for u in c.updates(quiet) if u["sessionUpdate"] == "agent_message_chunk"] == [
        "the report\n"]


def test_a_process_that_cannot_start_answers_with_an_error(client):
    def broken(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    c = client(popen=broken)
    session = c.new_session("/gone")
    error = c.answer(c.prompt(session, "go"))["error"]
    assert error["code"] == -32603
    assert "/gone" in error["message"]
    assert not [u for u in c.updates(session) if u["sessionUpdate"] == "tool_call"]
    # The session is not left busy by the failed start.
    assert c.answer(c.prompt(session, "again"))["error"]["code"] == -32603


def test_unknown_methods_notifications_and_garbage_are_answered_as_json_rpc_says(client):
    c = client()
    c.notify("session/whatever", {})  # a notification nobody knows: ignored
    c.raw("")  # a blank line is no message
    c.raw("{this is not json")
    c.raw("[1, 2]")
    unknown = c.request("session/whatever")
    prompt = c.request("session/prompt", {"sessionId": "nope", "prompt": [{"type": "text", "text": "x"}]})
    bad_blocks = c.request("session/prompt", {"sessionId": c.new_session(), "prompt": "text"})
    no_text = c.request("session/prompt", {"sessionId": c.new_session(), "prompt": [{"type": "image"}]})
    c.answer(no_text)
    errors = {m["id"]: m["error"] for m in c.out.messages() if "error" in m}
    assert [m["error"] for m in c.out.messages() if "error" in m and m["id"] is None] == [
        {"code": -32700, "message": "Parse error"}, {"code": -32600, "message": "Invalid Request"}]
    assert errors[unknown] == {"code": -32601, "message": "Method not found"}
    assert errors[prompt]["code"] == -32602
    assert errors[bad_blocks]["code"] == -32602
    assert errors[no_text]["code"] == -32602
    # Nothing else was said: not for the unknown notification, nor the blank line.
    assert len(c.out.messages()) == 2 + 4 + 2 * 2  # two garbage, four requests, two sessions' announcements and answers


def test_the_loop_ends_at_eof_only_after_the_running_prompts_finish(client):
    spawner = Spawner(hold=True, stdout="late\n")
    c = client(spawner)
    session = c.new_session()
    request = c.prompt(session, "go")
    c.out.wait_for(lambda m: m.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call")
    c._lines.put(None)  # the client closed its input
    assert c.thread.is_alive()
    spawner.procs[0].release()
    c.thread.join(WAIT_S)
    assert not c.thread.is_alive()
    assert c.answer(request)["result"] == {"stopReason": "end_turn"}


def test_the_subcommand_serves_the_protocol_on_stdout_alone(tmp_path):
    lines = "\n".join(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": {}})
                      for n, method in enumerate(["initialize", "authenticate"], 1)) + "\n"
    proc = subprocess.run([sys.executable, "-m", "cauce", "acp"], input=lines, capture_output=True, text=True,
                          check=False, timeout=30, cwd=tmp_path,
                          env={**os.environ, "PYTHONPATH": str(Path(cauce.__file__).parents[1])})
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[0]["result"]["agentInfo"]["version"] == cauce.__version__
