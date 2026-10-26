"""cauce against one plain Claude Code session, on the same build.

Real: each run is a fresh copy of the pricing engine (`build.repository`) and the same request
(`build.ASK`, `build.HARD` added for `--hard`). The hidden tests the workers never see judge the
result (`build.hidden`). Arms:

- `opus/low`, `opus/medium`, `sonnet/high`, ...: one `claude -p` at that model and effort, the
  request as its whole prompt, nothing of cauce around it;
- `cauce`: `cauce run <request> --verify 'make test'` in the copy, with a cauce home of its own
  (no history: every start comes from cauce's rules), the work judged on the branch it kept.

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

OUT = HERE / "results-versus.jsonl"
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


def cauce(where: Path, request: str) -> dict:
    home = Path(tempfile.mkdtemp(prefix="cauce-home-"))
    env = {**os.environ, "CAUCE_HOME": str(home), "CAUCE_AUTOWORK": "off", "CAUCE_NAMES": "off"}
    env.pop("CAUCE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    done = subprocess.run([str(ROOT / "bin" / "cauce"), "run", request, "--verify", "make test"], cwd=where,
                          env=env, capture_output=True, text=True, timeout=7200)
    db = sqlite3.connect(home / "cauce.db")
    task = db.execute("SELECT id, status, start_cell, final_cell, cost_usd FROM tasks WHERE source = 'cauce' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    attempts = db.execute("SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task[0],)).fetchone()[0] if task else 0
    db.close()
    shutil.rmtree(home, ignore_errors=True)
    if not task:
        return {"error": (done.stderr or done.stdout)[-300:]}
    branch = f"cauce/task-{task[0]}"
    if subprocess.run(["git", "rev-parse", "--verify", "-q", branch], cwd=where, capture_output=True).returncode == 0:
        subprocess.run(["git", "checkout", "-q", branch], cwd=where, check=True)
    return {"cost": round(float(task[4] or 0), 4), "status": task[1], "start": task[2], "final": task[3],
            "attempts": attempts}


def one(arm: str, variant: str) -> dict:
    where = build.repository(variant)
    began = time.monotonic()
    try:
        got = cauce(where, ask(variant)) if arm == "cauce" else plain(arm, where, ask(variant))
    except subprocess.TimeoutExpired:
        got = {"error": "timed out"}
    wall = time.monotonic() - began
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=600).returncode == 0
    row = {"arm": arm, "variant": variant, "wall_s": round(wall, 1), "visible": visible,
           "hidden": build.hidden(where, variant), "of": 10 if variant == "hard" else 7, **got}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def main(argv: list[str]) -> None:
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    if argv[0] == "report":
        rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
        print(f"{'build':<7}{'arm':<14}{'n':>3}{'right':>8}{'hidden':>9}{'wall p50':>10}{'mean':>7}{'$ p50':>8}"
              f"{'$ mean':>8}")
        for key in sorted({(r["variant"], r["arm"]) for r in rows}):
            rs = [r for r in rows if (r["variant"], r["arm"]) == key]
            right = sum(r["hidden"] == r["of"] for r in rs)
            costs = [r.get("cost", 0) for r in rs]
            print(f"{key[0]:<7}{key[1]:<14}{len(rs):>3}{right:>5}/{len(rs):<2}"
                  f"{sum(r['hidden'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
                  f"{median(r['wall_s'] for r in rs):>10.0f}{sum(r['wall_s'] for r in rs) / len(rs):>7.0f}"
                  f"{median(costs):>8.2f}{sum(costs) / len(costs):>8.2f}")
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
