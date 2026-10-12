"""A guard between a worker and an MCP server: the calls a worker nobody watches
must not make are refused here, not only asked of it in its prompt.

    python -m cauce.mcp_guard rastro -- rastro mcp --routines=off

It runs the server as a child and passes JSON-RPC lines both ways (MCP over
stdio is one JSON message per line). A `tools/call` the policy refuses never
reaches the server: the worker gets a tool error saying why. Everything else
passes unchanged. Standard library only, and no import of the rest of cauce:
it starts inside every worker that is handed the server.

The `rastro` policy keeps a worker to the app its task runs:

- a browser profile is refused: it holds the person's logins;
- writes and uploads are let through only to loopback hosts (localhost,
  127.x, ::1): the app under test, never a real site;
- saved routines and the profile list are refused: a routine can write with
  the person's logins;
- every call runs in rastro's session `cauce-worker`, so a worker never picks
  up a session of the person's, with what it was bound to.
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

SESSION = "cauce-worker"
#: rastro's own tools; any other name is a saved routine exposed as a tool.
RASTRO_TOOLS = frozenset({"rastro_open", "rastro_view", "rastro_read", "rastro_act", "rastro_navigate",
                          "rastro_effects", "rastro_trace", "rastro_request", "rastro_history", "rastro_detail",
                          "rastro_export"})
RASTRO_REFUSED = frozenset({"rastro_profiles", "rastro_routines", "rastro_routine_run"})


def loopback(host: str) -> bool:
    """A host a write may go to: this machine only."""
    value = host.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("@")[-1]
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    if value == "localhost" or value.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def rastro(name: str, arguments: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Why a call is refused (None when it is not), and its arguments as sent on."""
    args = dict(arguments)
    if name in RASTRO_REFUSED or name not in RASTRO_TOOLS:
        return (f"{name} is not for a worker: saved routines and browser profiles can act with the person's "
                "logins"), args
    if name == "rastro_open":
        if args.get("profile"):
            return "a browser profile holds the person's logins: open without one", args
        for key in ("allowWrite", "allowUpload"):
            hosts = args.get(key) or []
            if not isinstance(hosts, list):
                return f"{key} is a list of hosts", args
            others = [h for h in hosts if not isinstance(h, str) or not loopback(h)]
            if others:
                return (f"{key} reaches only the app on this machine (localhost); refused for "
                        f"{', '.join(map(str, others))}"), args
    args["session"] = SESSION
    return None, args


POLICIES: Mapping[str, Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]]] = {"rastro": rastro}


def screen(message: Any, policy: Callable[[str, Mapping[str, Any]], tuple[str | None, dict[str, Any]]]
           ) -> tuple[Any, list[dict[str, Any]]]:
    """A message from the worker: what goes on to the server (None for nothing),
    and the answers to send back for the calls refused."""
    if isinstance(message, list):
        onward, answers = [], []
        for item in message:
            sent, refused = screen(item, policy)
            if sent is not None:
                onward.append(sent)
            answers += refused
        return (onward or None), answers
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return message, []
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    name = params.get("name") if isinstance(params.get("name"), str) else ""
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    why, args = policy(name, arguments)
    if why is None:
        return {**message, "params": {**params, "arguments": args}}, []
    answer = {"jsonrpc": "2.0", "id": message.get("id"),
              "result": {"content": [{"type": "text", "text": f"cauce refused this call: {why}"}], "isError": True}}
    return None, [answer]


def relay(source: BinaryIO, sink: BinaryIO, lock: threading.Lock) -> None:
    for line in iter(source.readline, b""):
        with lock:
            sink.write(line)
            sink.flush()


def serve(policy_name: str, command: list[str], stdin: BinaryIO, stdout: BinaryIO) -> int:
    policy = POLICIES[policy_name]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    lock = threading.Lock()
    back = threading.Thread(target=relay, args=(child.stdout, stdout, lock), daemon=True)
    back.start()
    try:
        for line in iter(stdin.readline, b""):
            try:
                message = json.loads(line)
            except ValueError:
                child.stdin.write(line)
                child.stdin.flush()
                continue
            onward, answers = screen(message, policy)
            for answer in answers:
                with lock:
                    stdout.write(json.dumps(answer).encode() + b"\n")
                    stdout.flush()
            if onward is not None:
                child.stdin.write(json.dumps(onward).encode() + b"\n")
                child.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()
        code = child.wait()
        back.join(timeout=2)
    return code


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in POLICIES or argv[1] != "--":
        print(f"usage: python -m cauce.mcp_guard {{{','.join(POLICIES)}}} -- <server command>", file=sys.stderr)
        return 2
    return serve(argv[0], argv[2:], sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":  # pragma: no cover - the module is run as a server
    sys.exit(main(sys.argv[1:]))
