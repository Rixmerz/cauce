"""What the store says is broken: states cauce should never reach, read, never written.

Every rule here is a bug cauce shipped and found late, in real work, turned into a
query: a lane paused on a task that already passed, a workflow part that lost its mark
when it was resumed, an integrate step that joined fewer branches than its parts kept,
a worker whose death nobody resumed. A rule reads the store as it is (`broken`: work
is stuck now) or as it went over a window (`suspect`: something that should have
happened did not). No model, no git call, no write; `cauce doctor` prints them, the
session start names what is broken, and the scenario tests assert there is none.

A `suspect` rule names the release that fixes its bug (`FIXED_IN`), and reads it only from
what that release or a later one wrote (the `version` an event keeps): the records from
before it are counted apart as `fixed`, so what it reports is the bug coming back, never
the history the fix is for.
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

#: What a finding says of the work: stuck now, a sign over the window, or records from
#: before the release that fixes what they show.
BROKEN, SUSPECT, FIXED = "broken", "suspect", "fixed"
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
#: The release that fixes each suspect rule's bug: records written before it are history.
FIXED_IN = {
    "part-lost-its-mark": "0.29.2",
    "join-short-of-parts": "0.29.2",
    "attempts-with-no-session": "0.29.2",
    "turns-under-the-kind": "0.29.2",
    "format-failure-climbed": "0.30.1",
    "fix-round-off-base": "0.31.0",
    "revision-judged-twice": "0.32.0",
    "trial-never-sampled": "0.32.0",
    "note-proven-unbriefed": "0.32.0",
}
#: A candidate revision this old whose trial briefed no task, while its role's tasks here were briefed.
TRIAL_UNSAMPLED = timedelta(days=3)
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


def _release(version: object) -> tuple[int, ...]:
    """A version as numbers; none (a record from before events kept one) is the oldest."""
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(n) for n in found.groups()) if found else (0,)


def _fixed(rule: str, version: object) -> bool:
    """The record was written before the release that fixes its rule's bug."""
    return _release(version) < _release(FIXED_IN[rule])


def _version_of(store: Store, task_id: int) -> object:
    """The version of the latest record a task holds that keeps one."""
    found = [e for e in (store.last_event(task_id, kind) for kind in ("planned", "attempt_started", "finished"))
             if e is not None]
    return max(found, key=lambda e: e["id"])["data"].get("version") if found else None


def _history(rule: str, ids: list[int], repo: str | None) -> Finding:
    """Records a rule matched that the release fixing it never wrote: counted, never a sign."""
    return Finding(rule, FIXED, f"{len(ids)} record(s) from before {FIXED_IN[rule]}, the release that fixes it "
                                f"(latest #{max(ids)})",
                   "nothing to do: work run since shows whether it came back", max(ids), repo)


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
                    if _fixed("part-lost-its-mark", _version_of(store, t["id"])):
                        yield _history("part-lost-its-mark", [t["id"]], t["repo"])
                        continue
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
        if len(kept) > 1 and _fixed("join-short-of-parts", data.get("version")):
            yield _history("join-short-of-parts", [task["id"]], task["repo"])
        elif len(kept) > 1:
            yield Finding("join-short-of-parts", SUSPECT,
                          f"run {task['workflow_run']}: #{task['id']} passed without joining, while parts "
                          f"{', '.join(f'#{k}' for k in kept)} each kept a branch: the work after it misses some",
                          "the parts' branches were never merged; check what the run's last branch holds",
                          task["id"], task["repo"])


def attempts_with_no_session(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """Worktree attempts keep their session to be continued; when most keep none, none can be."""
    by_repo: dict[str | None, list[bool]] = {}
    before: dict[str | None, list[int]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        if not event["data"].get("base"):
            continue  # not in a worktree: no session is kept there
        attempt = store.attempt(event["task_id"], int(event["data"].get("seq") or 0))
        task = store.get_task(event["task_id"])
        if attempt is None or task is None or (attempt.get("passed") is None and attempt.get("failure") is None):
            continue
        if _fixed("attempts-with-no-session", event["data"].get("version")):
            if not attempt.get("session_id"):
                before.setdefault(task["repo"], []).append(task["id"])
            continue
        by_repo.setdefault(task["repo"], []).append(bool(attempt.get("session_id")))
    for repo, ids in before.items():
        yield _history("attempts-with-no-session", ids, repo)
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
    before: dict[str | None, list[int]] = {}
    for event in store.events_of_kinds(["attempt_started"], since=since):
        turns = event["data"].get("max_turns")
        task = store.get_task(event["task_id"]) if event["data"].get("seq") == 1 else None
        if not task or not task.get("kind") or not isinstance(turns, int) or _options(task).get("max_turns"):
            continue
        if turns < turns_for(task["kind"]) and _fixed("turns-under-the-kind", event["data"].get("version")):
            before.setdefault(task["repo"], []).append(task["id"])
        elif turns < turns_for(task["kind"]):
            under.setdefault((task["repo"], task["kind"]), []).append(task["id"])
    for repo, old in before.items():
        yield _history("turns-under-the-kind", old, repo)
    for (repo, kind), ids in under.items():
        yield Finding("turns-under-the-kind", SUSPECT,
                      f"{len(ids)} {kind} task(s) started under the kind's {turns_for(kind)} turns "
                      f"(latest #{max(ids)})",
                      "a standard or a history rule set it lower than the kind allows", max(ids), repo)


def format_failures_that_climbed(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A worker's answer of the wrong shape is no shallow work: it climbed effort anyway."""
    before: list[int] = []
    for event in store.events_of_kinds(["moved"], since=since):
        data = event["data"]
        if data.get("axis") != "effort" or data.get("trigger") != "inconclusive":
            continue
        attempt = store.attempt(event["task_id"], int(data.get("seq") or 0))
        if attempt and _FORMAT.search(attempt.get("summary") or "") and _fixed("format-failure-climbed",
                                                                               data.get("version")):
            before.append(event["task_id"])
        elif attempt and _FORMAT.search(attempt.get("summary") or ""):
            yield Finding("format-failure-climbed", SUSPECT,
                          f"#{event['task_id']} climbed {data.get('from_cell')} → {data.get('to_cell')} because its "
                          f"answer had the wrong shape: {str(attempt['summary'])[:120]}",
                          "the shape the brief asks for was missed; a stronger cell pays for a format, not for depth",
                          event["task_id"], None)
    if before:
        yield _history("format-failure-climbed", before, None)


def fix_rounds_off_their_base(store: Store, *, since: str, **_) -> Iterable[Finding]:
    """A fix's later round that started where the round before it started, not on what that
    round kept: the run's last branch misses the earlier round's fixes."""
    before: list[int] = []
    seen: set[int] = set()
    for event in store.events_of_kinds(["planned"], since=since):
        task = store.get_task(event["task_id"])
        spec = _options(task).get("ledger") or {} if task else {}
        if not task or task["id"] in seen or spec.get("role") != "fix" or int(spec.get("round") or 1) < 2:
            continue
        seen.add(task["id"])
        earlier = [t for t in store.run_tasks(task["workflow_run"]) if t["id"] < task["id"]
                   and t.get("workflow_step") == task.get("workflow_step") and t["status"] == "done"]
        if not earlier:
            continue
        prior = earlier[-1]
        kept = (store.final_event(prior["id"]) or {}).get("data") or {}
        if not kept.get("branch"):
            continue
        prior_base = (_options(prior).get("ledger") or {}).get("base")
        off = spec.get("base") != kept["tip"] if kept.get("tip") else spec.get("base") == prior_base
        if off and _fixed("fix-round-off-base", _version_of(store, task["id"])):
            before.append(task["id"])
        elif off:
            yield Finding("fix-round-off-base", SUSPECT,
                          f"run {task['workflow_run']}: fix round {spec.get('round')} #{task['id']} did not start "
                          f"from what round {int(spec.get('round')) - 1} #{prior['id']} kept on {kept['branch']}",
                          f"merge {kept['branch']} into the run's last branch before using it", task["id"],
                          task["repo"])
    if before:
        yield _history("fix-round-off-base", before, None)


def candidates_on_trial_at_once(store: Store, **_) -> Iterable[Finding]:
    """Two revisions of one role on trial in one repository: each would take the other's arm."""
    from cauce import revisions

    for repo, role in sorted({(r["repo"], r["role"]) for r in store.role_revisions()}):
        open_ = [p for p in revisions.state(store, repo, role)["proposals"] if p["trial"] is None]
        if len(open_) > 1:
            yield Finding("candidates-on-trial-at-once", BROKEN,
                          f"{role} has {len(open_)} revisions on trial at once here: "
                          + ", ".join(f"#{p['id']}" for p in open_),
                          "the newest is the one briefed; the others wait on a verdict they never get",
                          None, repo)


def revisions_judged_twice(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A role's revision given two verdicts of one kind: adopted and dropped, or kept and reverted."""
    before: list[int] = []
    rows = store.role_revisions(since=since or "")
    for revision in sorted({r["revision"] for r in rows if r["revision"] is not None}):
        mine = [r for r in rows if r["revision"] == revision]
        for pair in (("adopted", "dropped"), ("kept", "reverted")):
            twice = [r for r in mine if r["record"] in pair]
            if len(twice) < 2:
                continue
            if _fixed("revision-judged-twice", twice[-1]["version"]):
                before.append(revision)
                continue
            yield Finding("revision-judged-twice", SUSPECT,
                          f"{twice[0]['role']}'s revision #{revision} was judged {len(twice)} times: "
                          + ", ".join(r["record"] for r in twice),
                          "the first verdict stands; a second settle judged it again", None, twice[0]["repo"])
    if before:
        yield _history("revision-judged-twice", before, None)


def trials_never_sampled(store: Store, *, now: datetime, **_) -> Iterable[Finding]:
    """A revision on trial for days whose arms hold no task while its role's tasks here were
    briefed since: the trial is never given, and nothing is ever judged."""
    from cauce import revisions

    before: list[int] = []
    for repo, role in sorted({(r["repo"], r["role"]) for r in store.role_revisions()}):
        candidate = revisions.state(store, repo, role)["candidate"]
        at = _ts(candidate["at"]) if candidate else None
        if candidate is None or at is None or now - at < TRIAL_UNSAMPLED:
            continue
        briefs = store.briefs(repo)
        sampled = any(b["data"].get("revision_trial") == candidate["id"] for b in briefs)
        since = [b for b in briefs if b["data"].get("role") == role and str(b["ts"]) > candidate["at"]]
        if sampled or not since:
            continue
        if _fixed("trial-never-sampled", candidate["version"]):
            before.append(candidate["id"])
            continue
        yield Finding("trial-never-sampled", SUSPECT,
                      f"{role}'s revision #{candidate['id']} has been on trial since {candidate['at'][:10]}, and none "
                      f"of the {len(since)} {role} task(s) briefed here since was put in an arm",
                      "check that `role_trials` is on where the workers run (`cauce config role_trials`)", None, repo)
    if before:
        yield _history("trial-never-sampled", before, None)


def notes_proven_unbriefed(store: Store, *, since: str | None, **_) -> Iterable[Finding]:
    """A worker-learned note made current as proven, though fewer tasks than proving takes
    kept a brief that carried it: it was promoted on a guess, not on evidence. Proving
    exists since the release that names this rule, so every such note is a sign."""
    from cauce import notes

    briefs: dict[int, set[int]] | None = None
    for project in store.note_projects():
        for note in store.notes(project, states=["current"], limit=100_000):
            if note["author"] != "worker" or not str(note["state_reason"] or "").startswith(notes.PROVEN) or (
                    since and str(note["updated_at"]) < since):
                continue
            briefs = notes.carried(store) if briefs is None else briefs
            read = [t for t, ids in briefs.items() if note["id"] in ids and t != note["task_id"]]
            if len(read) < notes.PROVE_AFTER:
                yield Finding("note-proven-unbriefed", SUSPECT,
                              f"note #{note['id']} was made current as proven, but {len(read)} task(s) kept a brief "
                              f"that carried it (proving takes {notes.PROVE_AFTER})",
                              f"check it with `cauce notes` and drop it if it does not hold (#{note['id']})",
                              note["task_id"], project)


RULES: tuple[Callable[..., Iterable[Finding]], ...] = (
    lanes_paused_on_ended, running_with_no_process, queued_with_no_dispatcher, resumes_that_never_ran,
    parts_that_lost_their_mark, joins_short_of_their_parts, attempts_with_no_session, turns_under_the_kind,
    format_failures_that_climbed, fix_rounds_off_their_base, candidates_on_trial_at_once, revisions_judged_twice,
    trials_never_sampled, notes_proven_unbriefed,
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
    return sorted(found, key=lambda f: ((BROKEN, SUSPECT, FIXED).index(f.severity), f.rule, f.task_id or 0))


def text(found: list[Finding]) -> str:
    lines = []
    if not any(f.severity != FIXED for f in found):
        lines.append("cauce doctor: nothing broken, nothing suspect.")
    for severity in (BROKEN, SUSPECT):
        mine = [f for f in found if f.severity == severity]
        if mine:
            lines.append(f"{severity} ({len(mine)}):")
            lines += [f"  [{f.rule}] {f.what}\n      → {f.fix}" for f in mine]
    if history := [f for f in found if f.severity == FIXED]:
        lines.append("fixed since, history only:")
        lines += [f"  [{f.rule}] {f.what}" for f in history]
    return "\n".join(lines)
