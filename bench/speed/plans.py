"""Does a cheaper cell plan as well as `plan`'s first rung? (`matrix.LADDERS["plan"]`)

Real: one read-only `claude -p` planner per run on a fresh copy of the memory bench's harder pricing
engine (`bench/memory/hard`), asked to plan a rule whose traps are found only by reading the code:
the house rounding helper (`money.pct`, half up) beside a legacy one an old rule still uses, the
customer's real shape (the tier nested under `loyalty`, which may be null, written in capitals) and
a rule registered in two places (`RULES` and `rules.cfg`). A plan is scored by the traps it names
(`TRAPS`); right when it names all four. The arms are cells.

    python bench/speed/plans.py run opus/high,sonnet/high,sonnet/medium 3 4
    python bench/speed/plans.py report
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from cauce import grants, launch, orchestrate  # noqa: E402
from cauce.matrix import Cell  # noqa: E402

BASE = HERE.parent / "memory" / "hard"
OUT = HERE / "results-plans.jsonl"
MAX_TURNS = 30
ASK = "Plan, without changing any file: Add a pricing rule: silver-tier loyalty customers get 5% off the order."
#: What a plan must name to keep its builder out of each trap.
TRAPS = {
    "house rounding": re.compile(r"money\.pct|\bpct\(|\bpct\b|half[- _]?up", re.I),
    "nested tier, may be null": re.compile(r"loyalty[\"'\[\]\s\w.]{0,30}tier", re.I),
    "capital letters": re.compile(r"SILVER|upper|case[- ]insensitive|casefold|\.lower\(", re.I),
    "two registrations": re.compile(r"rules\.cfg", re.I),
}
NULL = re.compile(r"\bnull\b|\bNone\b|no loyalty|missing|absent|\.get\(", re.I)


def repository() -> Path:
    import subprocess
    import tempfile

    where = Path(tempfile.mkdtemp(prefix="tally-plan-")) / "repo"
    shutil.copytree(BASE, where)
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init"]):
        subprocess.run(argv, cwd=where, check=True)
    return where


def judge(text: str) -> dict:
    found = {name: bool(rx.search(text)) for name, rx in TRAPS.items()}
    found["nested tier, may be null"] = found["nested tier, may be null"] and bool(NULL.search(text))
    return {"named": sum(found.values()), "of": len(TRAPS), "right": all(found.values()), "traps": found}


def one(cell: str) -> dict:
    model, effort = cell.split("/")
    where = repository()
    prompt = orchestrate.brief(ASK, "plan", where, [], [], None, writes=False)
    spec = launch.LaunchSpec(prompt=prompt, cell=Cell(model, effort), launch_dir=where, max_turns=MAX_TURNS,
                             timeout_s=launch.timeout_for(MAX_TURNS), permission_mode=grants.mode_for(model),
                             disallowed_tools=launch.WRITE_TOOLS)
    began = time.monotonic()
    try:
        r = launch.run(spec)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        shutil.rmtree(where.parent, ignore_errors=True)
        return {"cell": cell, "error": str(exc)[:300]}
    row = {"cell": cell, "wall_s": round(time.monotonic() - began, 1), "turns": r.turns, "cost": round(r.cost_usd, 4),
           "claimed": r.passed, **judge(r.raw or r.summary)}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def report() -> None:
    rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip() and "error" not in x]
    print(f"{'cell':<15}{'n':>3}{'right':>8}{'traps':>9}{'wall p50':>10}{'turns':>7}{'$ p50':>8}")
    for cell in sorted({r["cell"] for r in rows}):
        rs = [r for r in rows if r["cell"] == cell]
        print(f"{cell:<15}{len(rs):>3}{sum(r['right'] for r in rs):>5}/{len(rs):<2}"
              f"{sum(r['named'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
              f"{median(r['wall_s'] for r in rs):>10.0f}{median(r['turns'] for r in rs):>7}"
              f"{median(r['cost'] for r in rs):>8.3f}")


def main(argv: list[str]) -> None:
    if argv[0] == "report":
        report()
        return
    cells = argv[1].split(",")
    reps = int(argv[2]) if len(argv) > 2 else 3
    jobs = [c for _ in range(reps) for c in cells]

    def go(cell: str) -> None:
        row = one(cell)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 4) as pool:
        list(pool.map(go, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
