"""cauce against one plain Claude Code session, on the same build.

Real: each run is a fresh copy of the pricing engine (`build.repository`) and the same request
(`build.ASK`, `build.HARD` added for `--hard`). The hidden tests the workers never see judge the
result (`build.hidden`). Arms:

- `opus/low`, `opus/medium`, `sonnet/high`, ...: one `claude -p` at that model and effort, the
  request as its whole prompt, nothing of cauce around it;
- `karpathy:<cell>`: the same session with `andrej-karpathy-skills`' CLAUDE.md (behavioural
  guidelines: think first, simplicity, surgical changes, verifiable goals) committed in the copy,
  where Claude Code reads it as the project's instructions (`KARPATHY`, a clone of that repository);
- `cauce`: `cauce run <request> --verify 'make test'` in the copy, with a cauce home of its own
  (no history: every start comes from cauce's rules), the work judged on the branch it kept. Its
  wall time ends when the command exits; a late independent check (`late_verifier`) that runs after
  it is waited for and kept apart (`late_s`, `late_passed`), never in the wall time.

The wall time is the whole command's, from start to its exit; the cost is what the CLI reports
(for cauce, the task's: classification, attempts and the independent check).

    python bench/speed/versus.py run cauce,opus/low,opus/medium 3 3 [--hard]
    python bench/speed/versus.py report
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402

from cauce import __version__  # noqa: E402

OUT = HERE / "results-versus.jsonl"
#: A clone of github.com/multica-ai/andrej-karpathy-skills, for the `karpathy:` arms.
KARPATHY = Path(os.environ.get("KARPATHY_SKILLS", "/home/user/multica-ai/andrej-karpathy-skills"))
TOOLS = ("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)", "Read", "Edit", "Write", "Glob", "Grep")


def ask(variant: str) -> str:
    return build.ASK.replace("\n\nAdd tests", build.HARD + "\n\nAdd tests") if variant == "hard" else build.ASK


def plain(cell: str, where: Path, request: str) -> dict:
    model, effort = cell.split("/")
    done = subprocess.run(["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json",
                           "--permission-mode", "acceptEdits", "--max-turns", str(build.MAX_TURNS),
                           "--allowedTools", *TOOLS],
                          input=request, cwd=where, capture_output=True, text=True, timeout=3600)
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        return {"error": (done.stderr or done.stdout)[-300:]}
    return {"cost": round(float(reply.get("total_cost_usd") or 0), 4), "turns": reply.get("num_turns")}


def cauce(where: Path, request: str, arm: str = "cauce") -> dict:
    """`cauce`, `cauce@<cell>` (a pinned start) and either with `-noverify` (no independent check)."""
    home = Path(tempfile.mkdtemp(prefix="cauce-home-"))
    env = {**os.environ, "CAUCE_HOME": str(home), "CAUCE_AUTOWORK": "off", "CAUCE_NAMES": "off"}
    env.pop("CAUCE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if arm.endswith("-noverify"):
        env["CAUCE_VERIFIER"] = "off"
        arm = arm.removesuffix("-noverify")
    start = ["--start", arm.split("@", 1)[1]] if "@" in arm else []
    done = subprocess.run([str(ROOT / "bin" / "cauce"), "run", request, "--verify", "make test", *start], cwd=where,
                          env=env, capture_output=True, text=True, timeout=7200)
    ended = time.monotonic()
    db = sqlite3.connect(home / "cauce.db")
    task = db.execute("SELECT id, status, start_cell, final_cell, cost_usd FROM tasks WHERE source = 'cauce' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    attempts = db.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task[0],)).fetchone()[0] if task else 0
    late = waited_late(db, task[0]) if task else {}
    if late:
        late["late_s"] = round(time.monotonic() - ended, 1)
        task = db.execute("SELECT id, status, start_cell, final_cell, cost_usd FROM tasks WHERE id = ?",
                          (task[0],)).fetchone()
    db.close()
    shutil.rmtree(home, ignore_errors=True)
    if not task:
        return {"error": (done.stderr or done.stdout)[-300:]}
    branch = f"cauce/task-{task[0]}"
    if subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=where, capture_output=True).returncode == 0:
        subprocess.run(["git", "checkout", "-q", branch], cwd=where, check=True)
    return {"cost": round(float(task[4] or 0), 4), "status": task[1], "start": task[2], "final": task[3],
            "attempts": attempts, **late}


def waited_late(db: sqlite3.Connection, task_id: int, within_s: float = 900) -> dict:
    """A late independent check (`late_verifier`) runs after `cauce run` exits: wait for its verdict, so
    the cauce home is not removed under it, and keep what it said apart from the foreground wall time."""
    if not db.execute("SELECT 1 FROM events WHERE task_id = ? AND kind = 'verifier_deferred'",
                      (task_id,)).fetchone():
        return {}
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        row = db.execute("SELECT data FROM events WHERE task_id = ? AND kind = 'verified' ORDER BY id DESC LIMIT 1",
                         (task_id,)).fetchone()
        if row and json.loads(row[0]).get("late"):
            data = json.loads(row[0])
            return {"late_passed": data.get("passed"), "late_blocking": data.get("blocking")}
        time.sleep(2)
    return {"late_passed": None}


def diffstat(where: Path) -> dict:
    """What the work changed against the commit it started from, all of it (new files too)."""
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    base = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=where, capture_output=True,
                          text=True).stdout.split()[0]
    out = subprocess.run(["git", "diff", "--cached", "--numstat", base], cwd=where, capture_output=True,
                         text=True).stdout.splitlines()
    rows = [line.split("\t") for line in out if line.count("\t") >= 2]
    added = sum(int(a) for a, _, _ in rows if a.isdigit())
    removed = sum(int(r) for _, r, _ in rows if r.isdigit())
    tests = sum(int(a) for a, _, f in rows if a.isdigit() and "test" in f)
    return {"files": len(rows), "added": added, "removed": removed, "added_tests": tests}


#: The medium build as the two tasks a session would send side by side: files apart, merged after.
SPLIT = (
    ("feature", "Add pricing rules: loyalty members get their tier's discount on the order (gold 10%, silver "
     "5%); cart lines tagged promo get 15% off; a line of 10 or more of one item gets 5% off that line. Add tests "
     "for them."),
    ("implement", "Add a printed receipt, `tally.receipt.receipt(cart, customer) -> str`, one row per line and "
     "one per discount the checkout applies (with its sign) and then the total, and a CSV export of an order for "
     "finance, `tally.export.rows(cart, customer) -> list[str]`, a header row, one row per line (item, qty, unit "
     "price, line total) and a total row. Take the discounts from the checkout as it applies them; do not add or "
     "change pricing rules. Add tests for them."),
)


def split(where: Path) -> dict:
    """`cauce-split`: the two tasks of `SPLIT` run at once, each with the kind a session gives it, in one cauce
    home; their branches merged into the checkout after (a conflict is recorded, nothing resolved)."""
    home = Path(tempfile.mkdtemp(prefix="cauce-home-"))
    env = {**os.environ, "CAUCE_HOME": str(home), "CAUCE_AUTOWORK": "off", "CAUCE_NAMES": "off"}
    env.pop("CAUCE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    runs = [subprocess.Popen([str(ROOT / "bin" / "cauce"), "run", text, "--verify", "make test", "--kind", kind],
                             cwd=where, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for kind, text in SPLIT]
    for run in runs:
        run.wait(timeout=7200)
    db = sqlite3.connect(home / "cauce.db")
    tasks = db.execute("SELECT id, status, cost_usd FROM tasks WHERE source = 'cauce' ORDER BY id").fetchall()
    db.close()
    shutil.rmtree(home, ignore_errors=True)
    git = ["git", "-c", "user.email=b@b", "-c", "user.name=b"]
    conflict = False
    for task_id, _, _ in tasks:
        merged = subprocess.run([*git, "merge", "--no-ff", "-q", "-m", f"task {task_id}", f"cauce/task-{task_id}"],
                                cwd=where, capture_output=True, text=True)
        if merged.returncode != 0:
            conflict = True
            subprocess.run(["git", "merge", "--abort"], cwd=where, capture_output=True)
    return {"cost": round(sum(float(c or 0) for _, _, c in tasks), 4), "status": [s for _, s, _ in tasks],
            "conflict": conflict}


def one(arm: str, variant: str) -> dict:
    where = build.repository(variant)
    if arm.startswith("karpathy:") or arm.endswith("+karpathy"):
        shutil.copy(KARPATHY / "CLAUDE.md", where / "CLAUDE.md")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=where, check=True)
        subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "--amend", "-qm", "init"],
                       cwd=where, check=True)
    began = time.monotonic()
    try:
        got = (split(where) if arm == "cauce-split"
               else cauce(where, ask(variant), arm.removesuffix("+karpathy")) if arm.startswith("cauce")
               else plain(arm.removeprefix("karpathy:"), where, ask(variant)))
    except subprocess.TimeoutExpired:
        got = {"error": "timed out"}
    # The wall time ends when the command exits: a late check waited for after it is `late_s`.
    wall = time.monotonic() - began - float(got.get("late_s") or 0)
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=600).returncode == 0
    label = arm.replace("cauce", f"cauce {__version__}", 1) if arm.startswith("cauce") else arm
    row = {"arm": label, "variant": variant, "wall_s": round(wall, 1), "foreground": True,
           "visible": visible,
           "hidden": build.hidden(where, variant), "of": 10 if variant == "hard" else 7, **got,
           **diffstat(where)}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def main(argv: list[str]) -> None:
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    if argv[0] == "report":
        rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
        for r in rows:
            r["arm"] = "cauce 0.49.0" if r["arm"] == "cauce" else r["arm"]  # rows from before versions were kept
            if r.get("late_s") and not r.get("foreground"):  # rows whose wall time held the late wait
                r["wall_s"] = round(r["wall_s"] - r["late_s"], 1)
        print(f"{'build':<7}{'arm':<34}{'n':>3}{'right':>8}{'hidden':>9}{'wall p50':>10}{'mean':>7}{'$ p50':>8}"
              f"{'$ mean':>8}{'+lines p50':>12}{'-lines p50':>12}")
        for key in sorted({(r["variant"], r["arm"]) for r in rows}):
            rs = [r for r in rows if (r["variant"], r["arm"]) == key]
            right = sum(r["hidden"] == r["of"] for r in rs)
            costs = [r.get("cost", 0) for r in rs]
            plus = [r["added"] for r in rs if "added" in r]
            minus = [r["removed"] for r in rs if "removed" in r]
            print(f"{key[0]:<7}{key[1]:<34}{len(rs):>3}{right:>5}/{len(rs):<2}"
                  f"{sum(r['hidden'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
                  f"{median(r['wall_s'] for r in rs):>10.0f}{sum(r['wall_s'] for r in rs) / len(rs):>7.0f}"
                  f"{median(costs):>8.2f}{sum(costs) / len(costs):>8.2f}"
                  f"{(median(plus) if plus else float('nan')):>12.0f}"
                  f"{(median(minus) if minus else float('nan')):>12.0f}")
        return
    arms, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    jobs = [a for _ in range(reps) for a in arms]

    def run(arm: str) -> None:
        row = one(arm, variant)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 3) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
