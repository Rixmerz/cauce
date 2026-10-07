"""The UI server: a local page that can launch paid work, so a web page must never reach it.

The envelope, kept from tasky's dashboard because it held:

- it binds 127.0.0.1 only, and refuses any request whose `Host` is not this
  server — a DNS-rebinding page arrives with its own host name;
- mutations need a token from a 0600 file, compared in constant time and
  re-read on every request, so deleting the file revokes every open tab;
- mutations are `POST` with a JSON body; every `OPTIONS` is refused, so no
  cross-origin page can get past a preflight;
- a strict content security policy, a 1 MiB body cap, socket timeouts, and
  bare 500s with the trace in a local log.

A server outlives plugin updates: it keeps the code it started with. So it
checks, with its sweep, whether Claude Code has installed a newer cauce, and
when it has, it runs that version's UI in its own place, on its own port.

Two of tasky's weak spots are designed out. The token never travels in a URL:
the page asks `/api/token`, which answers only a same-origin request (and a
cross-origin page could not read the answer anyway). And slow work never runs
in a request thread: a request records intent, a background thread or a
`cauce` process does the work. A GET never starts anything.
"""
from __future__ import annotations

import contextlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import signal
import threading
import time
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cauce import __version__, config, dispatch, flow, link, stops
from cauce.store import Store, home
from cauce.ui import api

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 1024 * 1024
SOCKET_TIMEOUT = 10
SWEEP_EVERY_S = 30
STREAM_SECONDS = 25
MAX_STREAMS = 8
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_STATIC_NAME = re.compile(r"^[a-z0-9_-]+\.(js|css|html|svg)$")
_TASK_ROUTE = re.compile(r"^/api/tasks/(\d+)$")
_CANCEL_ROUTE = re.compile(r"^/api/tasks/(\d+)/cancel$")
_DISMISS_ROUTE = re.compile(r"^/api/tasks/(\d+)/dismiss$")
_FORGET_ROUTE = re.compile(r"^/api/problems/(\d+)/forget$")
_SESSION_ROUTE = re.compile(r"^/api/sessions/([A-Za-z0-9_-]{1,128})$")
_NOTE_ROUTE = re.compile(r"^/api/notes/(\d+)/(ok|drop)$")


def token_path(root: Path) -> Path:
    return root / "ui-token"


def ensure_token(root: Path) -> str:
    path = token_path(root)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    root.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, root: Path) -> None:
        self.root = root
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self.local = threading.local()
        self.stop_event = threading.Event()
        super().__init__(("127.0.0.1", port), Handler)
        self.allowed_hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}
        ensure_token(root)

    def store(self) -> Store:
        """One connection per thread: sqlite3 objects never cross threads."""
        if getattr(self.local, "store", None) is None:
            self.local.store = Store(self.root / "cauce.db")
        return self.local.store

    def housekeeping(self) -> None:
        """The sweep, in the server, on a timer — never because a page is open.
        And the check for a newer cauce, which takes this server's place."""
        store = Store(self.root / "cauce.db")
        try:
            while not self.stop_event.wait(SWEEP_EVERY_S):
                try:
                    flow.sweep(store)
                    newer = newer_install(os.environ)
                    if newer is not None:
                        self.hand_over(*newer)
                        return
                except Exception:  # logged; the server keeps serving
                    self.log_error_trace("housekeeping")
        finally:
            store.close()

    def hand_over(self, version: str, launcher: Path) -> None:
        """Become the newer cauce's UI, in this process and on this port: an open
        page keeps its address, and reloads when it sees the version change."""
        self.log_note(f"cauce {version} is installed; this {__version__} server runs it in its place "
                      f"on port {self.server_address[1]}")
        self.stop_event.set()
        self.socket.close()
        os.execv(str(launcher), [str(launcher), "ui", "--port", str(self.server_address[1])])  # noqa: S606

    def log_note(self, text: str) -> None:
        try:
            with (self.root / "ui-errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} {text}\n")
        except OSError:
            pass

    def log_error_trace(self, where: str) -> None:
        try:
            with (self.root / "ui-errors.log").open("a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.now(UTC).isoformat(timespec='seconds')} {where}\n{traceback.format_exc()}\n")
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server: UIServer
    server_version = "cauce"
    sys_version = ""
    timeout = SOCKET_TIMEOUT

    def log_message(self, format: str, *args: Any) -> None:
        return  # quiet: the page polls

    # --- the envelope -----------------------------------------------------------

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _token_ok(self) -> bool:
        try:
            expected = token_path(self.server.root).read_text(encoding="utf-8").strip()
        except OSError:
            return False
        given = self.headers.get("X-Cauce-Token", "")
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _guard(self) -> bool:
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "unknown host")
            return False
        return True

    def _body(self) -> dict[str, Any] | None:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON only")
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, "expected an object")
            return None
        return data

    # --- verbs -----------------------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self._error(HTTPStatus.FORBIDDEN, "no cross-origin requests")

    def do_GET(self) -> None:
        self._handle(self._get)

    def do_HEAD(self) -> None:
        self._handle(self._get)

    def do_POST(self) -> None:
        self._handle(self._post)

    def _handle(self, method) -> None:
        try:
            if self._guard():
                method()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # a bare 500; the trace goes to the local log
            self.server.log_error_trace(f"{self.command} {self.path}")
            with contextlib.suppress(OSError):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    # --- reads -----------------------------------------------------------------------

    def _get(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        store = self.server.store()
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        if path == "/api/version":
            # Which cauce answers here: `cauce ui` asks when the port is taken, and
            # the page reloads when it changes.
            return self._json(HTTPStatus.OK, {"version": __version__})
        if path == "/api/token":
            # Only the page itself: a browser marks its own fetches same-origin, and
            # a cross-origin page could not read this answer even if it asked.
            if self.headers.get("Sec-Fetch-Site") != "same-origin":
                return self._error(HTTPStatus.FORBIDDEN, "same-origin only")
            return self._json(HTTPStatus.OK, {"token": ensure_token(self.server.root)})
        if path == "/api/projects":
            return self._json(HTTPStatus.OK, api.projects(store))
        if path == "/api/board":
            # One project at a time, always: every repository's cards at once is
            # a wall nobody reads.
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/board?repo=<key>")
            return self._json(HTTPStatus.OK, api.board(store, {repo_key}))
        if path == "/api/sessions":
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/sessions?repo=<key>")
            return self._json(HTTPStatus.OK, api.session_list(store, {repo_key}))
        if match := _SESSION_ROUTE.match(path):
            detail = api.session_detail(store, match.group(1))
            return self._json(HTTPStatus.OK, detail) if detail else self._error(HTTPStatus.NOT_FOUND, "no such session")
        if match := _TASK_ROUTE.match(path):
            detail = api.task_detail(store, int(match.group(1)))
            return self._json(HTTPStatus.OK, detail) if detail else self._error(HTTPStatus.NOT_FOUND, "no such task")
        if path == "/api/spend":
            return self._json(HTTPStatus.OK, api.spend(store, days=_int(query, "days", 7, 1, 365)))
        if path == "/api/routing":
            return self._json(HTTPStatus.OK, api.routing(store))
        if path == "/api/notes":
            repo_key = (query.get("repo") or [""])[0]
            if not repo_key:
                return self._error(HTTPStatus.BAD_REQUEST, "pick a project: /api/notes?repo=<key>")
            return self._json(HTTPStatus.OK, api.notes_view(
                store, repo_key, topic=(query.get("topic") or [""])[0], query=(query.get("q") or [""])[0],
                state=(query.get("state") or [""])[0]))
        if path == "/api/memory":
            return self._json(HTTPStatus.OK, api.memory(store, (query.get("q") or [""])[0]))
        if path == "/api/habits":
            return self._json(HTTPStatus.OK, api.habit_view(store))
        if path == "/api/events":
            return self._json(HTTPStatus.OK, store.events(after=_int(query, "after", 0, 0, 10**12), limit=500))
        if path == "/api/stream":
            return self._stream(_int(query, "after", 0, 0, 10**12))
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _static(self, name: str) -> None:
        if not _STATIC_NAME.match(name) or not (STATIC / name).is_file():
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), kind)

    def _stream(self, after: int) -> None:
        """Server-sent events for `STREAM_SECONDS`; the browser reconnects with the
        last id it saw. Bounded, so open tabs cannot exhaust the threads."""
        if not self.server.streams.acquire(blocking=False):
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "too many streams; poll /api/events")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            store = self.server.store()
            deadline = time.monotonic() + STREAM_SECONDS
            while time.monotonic() < deadline and not self.server.stop_event.is_set():
                for event in store.events(after=after, limit=200):
                    after = event["id"]
                    self.wfile.write(f"id: {after}\ndata: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                self.server.stop_event.wait(1)
        finally:
            self.server.streams.release()

    # --- writes ----------------------------------------------------------------------

    def _post(self) -> None:
        if not self._token_ok():
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token")
        data = self._body()
        if data is None:
            return None
        store = self.server.store()
        path = urlparse(self.path).path
        if match := _CANCEL_ROUTE.match(path):
            task = store.get_task(int(match.group(1)))
            if task is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such task")
            if task["status"] == "queued":
                stops.record(store, task["id"], stops.Stop("cancelled", "you cancelled it from the UI before it ran",
                                                           by="you", extra={"via": "ui"}))
            elif task["status"] == "running":
                store.request_cancel(task["id"], via="ui")
                if task["pid"]:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(int(task["pid"]), signal.SIGTERM)
            return self._json(HTTPStatus.OK, {"id": task["id"], "cancel": True})
        if match := _DISMISS_ROUTE.match(path):
            task = store.get_task(int(match.group(1)))
            if task is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such task")
            if stops.dismiss(store, task, via="ui") is None:
                return self._error(HTTPStatus.CONFLICT, f"task #{task['id']} is {task['status']}: it waits on no one")
            return self._json(HTTPStatus.OK, {"id": task["id"], "dismissed": True})
        if match := _FORGET_ROUTE.match(path):
            problem = store.forget_problem(int(match.group(1)))
            if problem is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such problem")
            return self._json(HTTPStatus.OK, {"id": problem["id"], "forgotten": True})
        if match := _NOTE_ROUTE.match(path):
            from cauce import notes

            note = store.get_note(int(match.group(1)))
            if note is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such note")
            if match.group(2) == "drop":
                store.update_note(note["id"], state="dropped", state_reason="dropped from the UI")
            else:
                place = next((p["dir"] for p in api.projects(store, enrolled_only=False)
                              if p["repo"] == note["project"] and p["exists"]), "")
                notes.confirm(store, note["id"], Path(place) if place else None)
            return self._json(HTTPStatus.OK, {"id": note["id"], "state": store.get_note(note["id"])["state"]})
        if path == "/api/lanes/unpause":
            repo_key = str(data.get("repo") or "")
            store.unpause_lane(repo_key)
            # Unpausing is the person saying "go on": the lane's dispatcher resumes.
            started = config.enabled("autowork", os.environ) and self._start_lane(store, repo_key)
            return self._json(HTTPStatus.OK, {"unpaused": repo_key, "started": bool(started)})
        if path == "/api/work":
            # Intent only: a detached `cauce work` does the work, not this thread.
            if not self._start_lane(store, str(data.get("repo") or "")):
                return self._error(HTTPStatus.CONFLICT, "nothing to start: no directory for that lane, or it runs")
            return self._json(HTTPStatus.ACCEPTED, {"started": True})
        return self._error(HTTPStatus.NOT_FOUND, "not found")


    def _start_lane(self, store: Store, repo_key: str) -> bool:
        queued = store.queued_in(repo_key or None)
        where = Path(queued[0]["cwd"]) if queued and queued[0]["cwd"] else None
        if where is None or not where.is_dir():
            return False
        return dispatch.start(where, self.server.root, repo_key or "*")


def _int(query: dict[str, list[str]], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int((query.get(key) or [default])[0])
    except ValueError:
        return default
    return max(low, min(high, value))


def newer_install(env: Mapping[str, str]) -> tuple[str, Path] | None:
    """The newest cauce Claude Code has installed, when it is newer than this one."""
    found = link.installed(env)
    own = link.parse_version(__version__)
    if not found or own is None or found[-1][0] <= own:
        return None
    version, launcher = found[-1]
    return ".".join(map(str, version)), launcher


def occupant(port: int) -> dict[str, Any] | None:
    """Who answers on a taken port: {"cauce": bool, "version": str | None}, or None.
    A cauce UI from before 0.4.1 answers as cauce, without a version."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/api/version", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read()
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    if not (res.getheader("Server") or "").startswith("cauce"):
        return {"cauce": False, "version": None}
    try:
        data = json.loads(body) if res.status == HTTPStatus.OK else {}
    except ValueError:
        data = {}
    version = data.get("version") if isinstance(data, dict) else None
    return {"cauce": True, "version": version if isinstance(version, str) else None}


def serve(port: int = 8790, root: Path | None = None, *, ready: threading.Event | None = None) -> UIServer:
    """Build the server and start its housekeeping; the caller runs `serve_forever`."""
    server = UIServer(port, root or home())
    threading.Thread(target=server.housekeeping, daemon=True).start()
    if ready is not None:
        ready.set()
    return server
