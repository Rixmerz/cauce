# The UI

A local web page, served by `cauce ui`, for someone running many tasks across
many repositories. It answers three questions in this order: **what needs me**,
**what is running and what is it costing**, and **is the router choosing well**.

It reads the same SQLite file every other part of cauce writes. It owns no
state of its own, so the CLI, the hooks and the page never disagree.

## Screens

| Screen | Shows | Actions |
|---|---|---|
| **Board** | columns that are states of attention — *Needs you* (failed, blocked, replan, needs approval, awaiting merge), *Running* (live cell, attempt n, cost so far), *Queued* per repository lane, *Done* as a diary by day — across repositories, filterable by repo and cell | queue a task, cancel, unpause a lane |
| **Task** | the attempt timeline: cell, turns, cost, outcome, the move and its reason; the plan's reasons and neighbours; messages; changed files; the branch; impact lines from livespec; dead ends shown to it | cancel; re-run pinned at a cell; copy the merge command |
| **Spend** | tokens and dollars by day, repository, model and cell; escalation rate and what escalations cost | — |
| **Routing** | the model × effort grid: per kind, where tasks started, where they passed, how often each cell escalated | — (the table is code; this screen is the evidence for changing it) |
| **Memory** | problems and fixes across repositories, dead ends first, recurring problems flagged, what worked instead | record a fix |
| **Habits** | mined sequences with their gates and score; proposals awaiting approval | none in the page: installing a hook is a CLI command a person types |

## Live updates

The page reads `GET /api/events?after=<id>` — the event table the core already
writes (`planned`, `attempt_started`, `attempt_finished`, `moved`, `finished`)
— as server-sent events, and falls back to polling that endpoint. A view never
polls the full state.

Housekeeping runs in the server, not in a request: a background thread sweeps
stale runs (a `running` task whose `cauce run` pid is gone becomes
`interrupted` and pauses its lane) and drains lanes when the dispatcher is on.
A GET never starts work.

## The security envelope

Kept from tasky's dashboard unchanged, because a local server that can launch
paid work must not be reachable by a web page the person happens to visit:

- binds `127.0.0.1` only, and allowlists the `Host` header against DNS
  rebinding;
- a random token in a `0600` file in cauce's home, compared with
  `hmac.compare_digest`, re-read on every request so deleting the file revokes
  every tab;
- mutations only as `POST`/`PATCH` with a JSON body and the token header;
  every `OPTIONS` is refused, which closes cross-site requests;
- a strict content security policy (no inline script, no third-party origin), a
  1 MiB body cap, socket timeouts, bare 500s with a local error log.

Two of tasky's weak spots are designed out: the token is never put in a URL
(the page reads it once from a same-origin bootstrap that checks the `Host`),
and slow work never runs in a request thread — the request records intent, a
worker thread or a `cauce run` process does the work.

## Shape of the code

- `src/cauce/ui/server.py` — the HTTP server, the envelope, the routes, the
  background housekeeping thread. Standard library only.
- `src/cauce/ui/api.py` — pure functions from the store to JSON. Every screen's
  data is testable without a socket.
- `src/cauce/ui/static/` — one HTML file, one CSS file and one small ES module
  per screen. No build step and no framework, and no single file that grows to
  thousands of lines.

## The terminal

`cauce board --json` prints the board's counts as typed JSON. A status line or
a mod reads fields, never a sentence: tasky's terminal board regex-matched a
line of prose, so rewording it blanked the status line.
