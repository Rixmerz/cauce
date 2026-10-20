"""Where a lesson belongs: the area it was met in, the role that met it, or everyone."""
from __future__ import annotations

import json

from cauce import cells, hooks
from cauce.store import Store

from .test_hooks import prompt


def test_a_path_is_in_one_area_and_a_change_in_its_specific_ones():
    assert cells.of_path("islands/Board.tsx") == "ui" and cells.of_path("src/auth/login.py") == "security"
    assert cells.of_path("migrations/0003_add.sql") == "data" and cells.of_path("tests/test_x.py") == "tests"
    assert cells.of_path("docs/guide.md") == "docs" and cells.of_path("src/cauce/flow.py") == "code"
    # A feature's tests and docs travel with it: its area is where the work was.
    assert cells.of_paths(["islands/Board.tsx", "tests/test_board.py", "README.md"]) == ("ui",)
    assert cells.of_paths(["src/x.py", "tests/test_x.py"]) == ("code",)
    assert cells.of_paths(["tests/test_x.py", "docs/a.md"]) == ("docs", "tests")
    assert cells.of_paths([]) == ()
    # A request reaches every area its paths are in, and the ones its words are about.
    assert cells.of_request("fix the login password check", "bugfix", ["tests/test_login.py"]) == (
        "security", "tests")
    assert "ui" in cells.of_request("the modal button overlaps on small screens", "bugfix")


def _met(store: Store, problem: int, *, role: str | None, areas: tuple[str, ...], task: int | None = 1) -> None:
    store.add_fix(problem, f"tried something by {role}", "failed", repo="r", why="code_bug", task_id=task,
                  role=role, areas=areas)


def test_a_failure_is_its_cell_s_and_shared_only_when_cells_with_nothing_in_common_met_it(store: Store):
    ui = store.open_problem("the board header overlaps the filters on phones", repo="r")
    _met(store, ui, role="implementer", areas=("ui",))
    problem = store.problem(ui)
    assert cells.scope(problem, role="security-auditor", areas=("security",)) is None
    assert cells.scope(problem, role="reviewer", areas=("ui", "code")) == "area"
    assert cells.scope(problem, role="implementer", areas=()) == "role"
    # The same task meeting it again spans what it spans: still one cell.
    _met(store, ui, role="implementer", areas=("ui", "data"))
    assert cells.scope(store.problem(ui), role="security-auditor", areas=("security",)) is None
    # Another task met it in an area the first never was in: everyone's now.
    _met(store, ui, role="debugger", areas=("security",), task=2)
    assert cells.scope(store.problem(ui), role="security-auditor", areas=()) == "shared"
    # Two tasks in a common area stay that area's.
    data = store.open_problem("the backfill drops rows with a null owner", repo="r")
    _met(store, data, role="implementer", areas=("data",), task=3)
    _met(store, data, role="debugger", areas=("data", "code"), task=4)
    assert cells.scope(store.problem(data), role=None, areas=()) is None
    # What a person wrote down is everyone's.
    kept = store.open_problem("the proxy drops long uploads", repo="r")
    _met(store, kept, role=None, areas=(), task=None)
    assert cells.scope(store.problem(kept), role=None, areas=()) == "shared"


def test_a_worker_sees_its_cell_s_dead_ends_and_the_session_only_the_shared_ones(store: Store):
    ui = store.open_problem("dashboard chart tooltip clipped inside the card", repo="r")
    _met(store, ui, role="implementer", areas=("ui",))
    text = "the dashboard chart tooltip is clipped inside the card again"
    shown = cells.dead_ends(store, text, repo="r", role="reviewer", areas=("ui",))
    assert [d["scope"] for d in shown] == ["area"]
    assert cells.dead_ends(store, text, repo="r", role="security-auditor", areas=("security",)) == []
    assert cells.dead_ends(store, text, repo="r", shared_only=True) == []
    assert cells.recent_shared(store, repo="r") == []
    _met(store, ui, role="debugger", areas=("data",), task=9)
    assert {d["scope"] for d in cells.dead_ends(store, text, repo="r", shared_only=True)} == {"shared"}
    assert cells.recent_shared(store, repo="r")[0]["scope"] == "shared"


def test_the_prompt_hook_tells_the_session_only_what_is_shared(store: Store):
    ui = store.open_problem("websocket reconnect loop in the dashboard", repo="github.com/x/y")
    _met(store, ui, role="implementer", areas=("ui",))
    assert prompt(store, "the websocket reconnect loop is back in the dashboard") is None


def test_a_workflow_step_is_matched_by_its_goal_never_its_template(store: Store):
    run = store.create_run(name="feature", scope="bundled", source="x", definition="{}",
                           inputs=json.dumps({"goal": "Export invoices as CSV from the admin list"}))
    step = store.create_task("[feature#1 build] Build it, with its tests", status="done", source="cauce",
                             repo="r", workflow_run=run["id"])
    assert cells.memory_text(store, step) == "Export invoices as CSV from the admin list"
    empty = store.create_run(name="feature", scope="bundled", source="x", definition="{}", inputs="{}")
    other = store.create_task("plain text", status="done", source="cauce", repo="r", workflow_run=empty["id"])
    assert cells.memory_text(store, other) == "plain text"
    assert cells.memory_text(store, {"title": "t"}) == "t"


def test_a_word_every_problem_holds_is_no_match(store: Store):
    # Twelve runs of one workflow share their step's template; only the goal tells them apart.
    for i in range(12):
        p = store.open_problem(f"[feature#{i} build] Build it, with its tests", repo="r",
                               symptom=f"Build this feature: subject{i} widget{i} gadget{i}. Read CLAUDE.md first")
        store.add_fix(p, "tried", "failed", repo="r", task_id=i + 1, role="implementer", areas=("code",))
    template = "Build this feature: something else entirely. Read CLAUDE.md first, build it with its tests"
    assert store.dead_ends(template, repo="r", strict=True) == []
    assert store.dead_ends("Build this feature: subject3 widget3 gadget3", repo="r", strict=True)[0][
        "problem"] == "[feature#3 build] Build it, with its tests"
    assert store.dead_ends("subject3 alone", repo="r", strict=True) == []  # one distinctive word is too little


def test_fixes_recorded_before_scopes_get_theirs_once(store: Store):
    task = store.create_task("fix the login redirect", status="done", source="cauce", repo="r", kind="bugfix")
    store.add_event(task["id"], "planned", kind="bugfix", role="debugger")
    store.add_attempt(task["id"], cell="sonnet/high", max_turns=80, passed=0, failure="code_bug",
                      changed_paths=json.dumps(["src/auth/redirect.py", "tests/test_redirect.py"]))
    bare = store.create_task("tidy the parser", status="done", source="cauce", repo="r", kind="refactor")
    p = store.open_problem("login redirect", repo="r")
    store.add_fix(p, "a", "failed", repo="r", task_id=task["id"])
    store.add_fix(p, "b", "failed", repo="r", task_id=bare["id"])
    store.add_fix(p, "c", "failed", repo="r", task_id=999)  # its task is gone
    assert cells.backfill(store) == 2 and cells.backfill(store) == 0
    fixes = {f["description"]: f for f in store.problem(p)["fixes"]}
    assert (fixes["a"]["role"], fixes["a"]["areas"]) == ("debugger", "security")
    assert (fixes["b"]["role"], fixes["b"]["areas"]) == (None, "")
    assert fixes["c"]["areas"] is None


def test_settling_scopes_the_old_fixes(store: Store):
    from cauce import evolution

    evolution.settle(store)
    assert store.meta(cells.BACKFILL_KEY) == "0"
    assert hooks  # the hooks import cells lazily; the module stays light
