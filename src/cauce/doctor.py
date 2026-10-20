"""What the store says is broken: states cauce should never reach, read, never written.

Every rule here is a bug cauce shipped and found late, in real work, turned into a
query: a lane paused on a task that already passed, a workflow part that lost its mark
when it was resumed, an integrate step that joined fewer branches than its parts kept,
a worker whose death nobody resumed. A rule reads the store as it is (`broken`: work
is stuck now) or as it went over a window (`suspect`: something that should have
happened did not). No model, no git call, no write; `cauce doctor` prints them, the
session start names what is broken, and the scenario tests assert there is none.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from cauce import after
from cauce.matrix import turns_for
from cauce.store import Store

#: What a finding says of the work: stuck now, or a sign over the window.
BROKEN, SUSPECT = "broken", "suspect"
#: How far back the `suspect` rules read by default.
WINDOW_DAYS = 2
#: Queued work no dispatcher took for this long, with nothing running in its lane, is stuck.
QUEUED_STUCK = timedelta(minutes=30)
#: An automatic resume that started no attempt after this long was refused or died.
RESUME_SILENT = timedelta(minutes=10)
#: A running task whose process is gone and that said nothing for this long.
RUNNING_SILENT = timedelta(minutes=2)
#: Worktree attempts with no session id at this share or more (over at least as many) mean no
#: attempt can be continued.
SESSIONLESS_SHARE, SESSIONLESS_MIN = 0.5, 3
#: A summary that says a worker's answer had the wrong shape, not that its work was wrong.
_FORMAT = re.compile(r"cannot run in parts|no result block|two result blocks|could not be read", re.I)
#: Statuses a task leaves the board with: nothing it paused waits on it any more.
_ENDED = ("done", "dismissed", "superseded", "cancelled")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    what: str
    #: What clears it, as a command or a sentence.
    fix: str
    task_id: int | None = None
    repo: str | None = None

    def data(self) -> dict:
        return asdict(self)


def _ts(value: object) -> datetime | None:
    try:
        found = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _options(task: Mapping) -> dict:
    try:
        found = json.loads(task.get("options") or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


# --- broken: work stuck now --------------------------------------------------------------


def lanes_paused_on_ended(store: Store, **_) -> Iterable[Finding]:
    """A lane paused on a task that left the board waits on nothing: its queue never starts."""
    for lane in store.lanes():
        found = re.match(r"task #(\d+)\b", str(lane.get("reason") or "")) if lane.get("paused") else None
        task = store.get_task(int(found.group(1))) if found else None
        if task and task["status"] in _ENDED:
            yield Finding("lane-paused-on-ended", BROKEN,
                          f"the lane is paused on #{task['id']}, which is {task['status']}: "
                          f"{lane['queued']} queued task(s) wait on nothing",
                          f"cauce lanes --unpause {lane['repo']}", task["id"], lane["repo"])


def running_with_no_process(store: Store, *, now: datetime, alive: Callable[[int | None], bool],
                            **_) -> Iterable[Finding]:
    """A task marked running whose process is gone and that has said nothing since."""
    for task in store.list_tasks(status=["running"], source=("cauce", "queue"), limit=500):
        last = _ts(task.get("updated_at"))
        if task.get("pid") and not alive(task["pid"]) and last and now - last > RUNNING_SILENT:
            yield Finding("running-with-no-process", BROKEN,
                          f"#{task['id']} is running, but its process {task['pid']} is gone",
                          "the next turn's hooks sweep it; if they do not, the sweep is broken",
                          task["id"], task["repo"])


def queued_with_no_dispatcher(store: Store, *, now: datetime, **_) -> Iterable[Finding]:
    """Queued work that may run, in an open lane where nothing runs, that nobody started."""
    paused = {lane["repo"] for lane in store.lanes() if lane.get("paused")}
    running = {t["repo"] for t in store.list_tasks(status=["running"], source=("cauce", "queue"), limit=500)}
    for task in store.list_tasks(status=["queued"], limit=500):
        since = _ts(task.get("updated_at"))
        if (task["repo"] in paused or task["repo"] in running or after.held(task) or since is None
                or now - since < QUEUED_STUCK):
            continue
        yield Finding("queued-with-no-dispatcher", BROKEN,
                      f"#{task['id']} has waited {int((now - since).total_seconds() // 60)} min in an open lane "
                      "with nothing running", f"cauce work --repo {task.get('cwd') or task['repo']}",
                      task["id"], task["repo"])


def resumes_that_never_ran(store: Store, *, now: datetime, since: str, **_) -> Iterable[Finding]:
    """An automatic resume that started no attempt: refused, or died before one."""
    for event in store.events_of_kinds(["auto_resumed"], since=since):
        task = store.get_task(event["task_id"])
        at = _ts(event["ts"])
        if task is None or task["status"] in _ENDED or at is None or now - at < RESUME_SILENT:
            continue
        started = store.last_event(task["id"], "attempt_started")
        if started is None or started["id"] < event["id"]:
            yield Finding("resume-never-ran", BROKEN,
                          f"#{task['id']} was resumed on its own {int((now - at).total_seconds() // 60)} min ago and "
                          f"no attempt started ({task['status']}); its one automatic try is spent",
                          f"cauce resume {task['id']} --unattended; read ~/.local/share/cauce/work/auto-resume.log",
                          task["id"], task["repo"])


# --- suspect: what went wrong over the window ---------------------------------------------


def parts_that_lost_their_mark(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A step run in parts with a task that is no part any more: its integrate drops the others."""
    runs = {e["task_id"]: None for e in store.events_of_kinds(["finished"], since=since)}
    seen: set[int] = set()
    for task_id in runs:
        task = store.get_task(task_id)
        run = task.get("workflow_run") if task else None
        if not run or run in seen:
            continue
        seen.add(run)
        steps: dict[str, list[dict]] = {}
        for t in store.run_tasks(run):
            steps.setdefault(t["workflow_step"], []).append(t)
        for step, tasks in steps.items():
            marked = [t for t in tasks if _options(t).get("part")]
            for t in tasks:
                if marked and not _options(t).get("part"):
                    yield Finding("part-lost-its-mark", SUSPECT,
                                  f"run {run}: #{t['id']} runs step {step!r} with no part beside "
                                  f"{len(marked)} part(s): the step reads as it alone",
                                  "a resume or a rewrite dropped `part` from its options", t["id"], t["repo"])


def joins_short_of_their_parts(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """An integrate step that skipped its join while more than one part kept a branch."""
    for event in store.events_of_kinds(["finished"], since=since):
        data = event["data"]
        # A ledger step skipped for want of findings joins nothing: only an integrate step's skip counts.
        task = store.get_task(event["task_id"]) if data.get("skipped") and "ledger" not in data else None
        if not task or not task.get("workflow_run"):
            continue
        kept = []
        for t in store.run_tasks(task["workflow_run"]):
            if t["id"] < task["id"] and _options(t).get("part") is not None:
                ended = store.final_event(t["id"]) or {}
                if (ended.get("data") or {}).get("branch"):
                    kept.append(t["id"])
        if len(kept) > 1:
            yield Finding("join-short-of-parts", SUSPECT,
                          f"run {task['workflow_run']}: #{task['id']} passed without joining, while parts "
                          f"{', '.join(f'#{k}' for k in kept)} each kept a branch: the work after it misses some",
                          "the parts' branches were never merged; check what the run's last branch holds",
                          task["id"], task["repo"])


def attempts_with_no_session(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """Worktree attempts keep their session to be continued; when most keep none, none can be."""
    by_repo: dict[str | None, list[bool]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        if not event["data"].get("base"):
            continue  # not in a worktree: no session is kept there
        attempt = store.attempt(event["task_id"], int(event["data"].get("seq") or 0))
        task = store.get_task(event["task_id"])
        if attempt is None or task is None or (attempt.get("passed") is None and attempt.get("failure") is None):
            continue
        by_repo.setdefault(task["repo"], []).append(bool(attempt.get("session_id")))
    for repo, kept in by_repo.items():
        missing = kept.count(False)
        if len(kept) >= SESSIONLESS_MIN and missing / len(kept) >= SESSIONLESS_SHARE:
            yield Finding("attempts-with-no-session", SUSPECT,
                          f"{missing} of {len(kept)} worktree attempts kept no session id: more turns or more "
                          "effort cannot continue a worker, every one starts fresh",
                          "a worker that inherits the host's session id reports it as its own "
                          "(launch.HOST_SESSION_VARS)", None, repo)


def turns_under_the_kind(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """First attempts that started under their kind's turns, with no `--max-turns` asking for
    it: one finding per repository and kind."""
    under: dict[tuple[str | None, str], list[int]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        turns = event["data"].get("max_turns")
        task = store.get_task(event["task_id"]) if event["data"].get("seq") == 1 else None
        if not task or not task.get("kind") or not isinstance(turns, int) or _options(task).get("max_turns"):
            continue
        if turns < turns_for(task["kind"]):
            under.setdefault((task["repo"], task["kind"]), []).append(task["id"])
    for (repo, kind), ids in under.items():
        yield Finding("turns-under-the-kind", SUSPECT,
                      f"{len(ids)} {kind} task(s) started under the kind's {turns_for(kind)} turns "
                      f"(latest #{max(ids)})",
                      "a standard or a history rule set it lower than the kind allows", max(ids), repo)


def format_failures_that_climbed(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A worker's answer of the wrong shape is no shallow work: it climbed effort anyway."""
    for event in store.events_of_kinds(["moved"], since=since):
        data = event["data"]
        if data.get("axis") != "effort" or data.get("trigger") != "inconclusive":
            continue
        attempt = store.attempt(event["task_id"], int(data.get("seq") or 0))
        if attempt and _FORMAT.search(attempt.get("summary") or ""):
            yield Finding("format-failure-climbed", SUSPECT,
                          f"#{event['task_id']} climbed {data.get('from_cell')} → {data.get('to_cell')} because its "
                          f"answer had the wrong shape: {str(attempt['summary'])[:120]}",
                          "the shape the brief asks for was missed; a stronger cell pays for a format, not for depth",
                          event["task_id"], None)


RULES: tuple[Callable[..., Iterable[Finding]], ...] = (
    lanes_paused_on_ended, running_with_no_process, queued_with_no_dispatcher, resumes_that_never_ran,
    parts_that_lost_their_mark, joins_short_of_their_parts, attempts_with_no_session, turns_under_the_kind,
    format_failures_that_climbed,
)


def check(store: Store, *, repo: str | None = None, days: int = WINDOW_DAYS, now: datetime | None = None,
          alive: Callable[[int | None], bool] | None = None) -> list[Finding]:
    """Every finding of every rule, broken first; `repo` keeps one repository's (and the
    ones no repository owns)."""
    from cauce import flow

    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat() if days else None
    found: list[Finding] = []
    for rule in RULES:
        found.extend(rule(store, now=now, since=since, alive=alive or flow.alive))
    if repo is not None:
        found = [f for f in found if f.repo in (repo, None)]
    return sorted(found, key=lambda f: (f.severity != BROKEN, f.rule, f.task_id or 0))


def text(found: list[Finding]) -> str:
    if not found:
        return "cauce doctor: nothing broken, nothing suspect."
    lines = []
    for severity in (BROKEN, SUSPECT):
        mine = [f for f in found if f.severity == severity]
        if mine:
            lines.append(f"{severity} ({len(mine)}):")
            lines += [f"  [{f.rule}] {f.what}\n      → {f.fix}" for f in mine]
    return "\n".join(lines)
