"""Does a worker need to run the full check before it reports, when cauce runs it right after?

Real: one `claude -p` worker per run, started by cauce's `launch.run`, on a fresh copy of the
pricing engine with a slow suite: `SLOW_FILES` test files that take `SLOW_S` seconds each, as a
real project's do. The check is that suite. The worker gets cauce's own brief lines for it
(`orchestrate.brief`), in one of two arms:

- `self`: as cauce briefed before, "run it yourself once, at the end, before you report";
- `cauce`: "do not run it yourself: cauce runs it as soon as you report and hands you its failures".

After the worker, cauce's check runs. A red one continues the worker's own session with the
failing output (`launch.CONTINUE_PROMPT`, as more turns do), at most `ROUNDS` times. Hidden tests
the worker never sees judge the result. The wall time is the worker's, the checks' and every
continuation's, until the check passes or the rounds run out.

    python bench/speed/handoff.py run self,cauce 4 4 [--hard]
    python bench/speed/handoff.py report
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import build  # noqa: E402

from cauce import grants, launch, orchestrate, usage  # noqa: E402
from cauce.matrix import Cell  # noqa: E402

CHECK = "PYTHONPATH=src python3 -m pytest -q tests"
SLOW_FILES, SLOW_S = 12, 5
ROUNDS = 2
CELL = Cell("sonnet", "high")
#: The brief's last line before 0.47, and cauce's own since.
OLD = "Run it yourself once, at the end, before you report."
NEW = "Do not run it yourself before you report: cauce runs it as soon as you do, and hands you its failures to fix."


def slow_suite(where: Path) -> None:
    for n in range(SLOW_FILES):
        (where / "tests" / f"test_slow_{n:02}.py").write_text(
            "import time\n\nfrom tally.money import pct\n\n\n"
            f"def test_settles_{n:02}():\n    time.sleep({SLOW_S})\n    assert pct is not None\n")
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "slow suite"], cwd=where,
                   check=True)


def check(where: Path) -> tuple[bool, str, float]:
    began = time.monotonic()
    done = subprocess.run(["python3", "-m", "pytest", "-q", "tests"], env={**os.environ, "PYTHONPATH": "src"},
                          cwd=where, capture_output=True, text=True, timeout=900)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:], time.monotonic() - began


def worker_check_runs(session: str) -> int:
    """How many times the worker ran the whole suite itself, read from its transcript."""
    path = usage.transcript_of(session, None) if session else None
    if not path or not Path(path).exists():
        return -1
    runs = 0
    for line in Path(path).read_text().splitlines():
        if '"tool_use"' in line and "pytest" in line:
            for block in (json.loads(line).get("message") or {}).get("content") or []:
                cmd = (block.get("input") or {}).get("command", "") if isinstance(block, dict) else ""
                if "pytest" in cmd and "test_" not in cmd.split("pytest", 1)[1]:
                    runs += 1
    return runs


def one(arm: str, variant: str) -> dict:
    where = build.repository(variant)
    slow_suite(where)
    ask = build.ASK.replace("\n\nAdd tests for it, and run the suite before you stop.", "\n\nAdd tests for it.")
    if variant == "hard":
        ask = ask.replace("\n\nAdd tests", build.HARD + "\n\nAdd tests")
    prompt = orchestrate.brief(ask, "feature", where, [], [], CHECK, True)
    assert NEW in prompt, "the brief no longer says what this benchmark measures"
    if arm == "self":
        prompt = prompt.replace(NEW, OLD)
    spec = launch.LaunchSpec(prompt=prompt, cell=CELL, launch_dir=where, max_turns=build.MAX_TURNS,
                             timeout_s=launch.timeout_for(build.MAX_TURNS), permission_mode=grants.mode_for(CELL.model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)",
                                            "Bash(PYTHONPATH=src python3:*)"),
                             persist_session=True)
    began = time.monotonic()
    rounds, cost, turns, self_runs, check_s = 0, 0.0, 0, 0, 0.0
    try:
        r = launch.run(spec)
        while True:
            cost += r.cost_usd
            turns += r.turns
            self_runs += max(worker_check_runs(r.session_id), 0)
            ok, out, took = check(where)
            check_s += took
            if ok or rounds >= ROUNDS or not r.session_id:
                break
            rounds += 1
            r = launch.run(dataclasses.replace(spec, resume_session=r.session_id,
                                               resume_note=f"cauce's check failed:\n{out}"))
    except Exception as exc:  # a launch that broke is recorded, never retried here
        return {"arm": arm, "variant": variant, "error": str(exc)[:300]}
    wall = time.monotonic() - began
    row = {"arm": arm, "variant": variant, "wall_s": round(wall, 1), "check_s": round(check_s, 1),
           "rounds": rounds, "green": ok, "turns": turns, "cost": round(cost, 4), "self_runs": self_runs,
           "hidden": build.hidden(where, variant), "of": 10 if variant == "hard" else 7}
    subprocess.run(["rm", "-rf", str(where.parent)])
    return row


def main(argv: list[str]) -> None:
    out = HERE / "results-handoff.jsonl"
    variant = "hard" if "--hard" in argv else "plain"
    argv = [a for a in argv if a != "--hard"]
    if argv[0] == "report":
        rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip() and "error" not in x]
        print(f"{'variant':<8}{'arm':<7}{'n':>3}{'right':>7}{'green':>7}{'wall p50':>10}{'mean':>7}{'rounds':>8}"
              f"{'own runs':>10}{'turns':>7}{'$ p50':>7}")
        for key in sorted({(r["variant"], r["arm"]) for r in rows}):
            rs = [r for r in rows if (r["variant"], r["arm"]) == key]
            right = sum(r["hidden"] == r["of"] for r in rs)
            green = sum(r["green"] for r in rs)
            print(f"{key[0]:<8}{key[1]:<7}{len(rs):>3}{right:>4}/{len(rs):<2}{green:>4}/{len(rs):<2}"
                  f"{median(r['wall_s'] for r in rs):>10.0f}{sum(r['wall_s'] for r in rs) / len(rs):>7.0f}"
                  f"{sum(r['rounds'] for r in rs):>8}{sum(r['self_runs'] for r in rs):>10}"
                  f"{median(r['turns'] for r in rs):>7}{median(r['cost'] for r in rs):>7.2f}")
        return
    arms, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    jobs = [a for _ in range(reps) for a in arms]

    def run(arm: str) -> None:
        row = one(arm, variant)
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 4) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
