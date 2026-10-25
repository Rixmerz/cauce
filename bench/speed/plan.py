"""Does a plan for parts need `max` effort, or does `xhigh` give parts as good, sooner?

Real: the plan step of `feature-parallel` asked of one worker (`LaunchSpec.parts`), read strictly
by cauce's own `launch.read_parts`, on `bench/memory/hard` and the build of `build.py`.

    python bench/speed/plan.py run opus/xhigh,opus/max 3 3
    python bench/speed/plan.py report
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import build  # noqa: E402

from cauce import grants, launch, workflows  # noqa: E402
from cauce.matrix import Cell  # noqa: E402


def one(cell: str) -> dict:
    model, effort = cell.split("/")
    where = build.repository()
    plan = next(s for s in workflows.load("feature-parallel", where, "bundled")["steps"] if s["id"] == "plan")
    prompt = plan["prompt"].replace("{goal}", build.ASK)
    spec = launch.LaunchSpec(prompt=prompt, cell=Cell(model, effort), launch_dir=where, max_turns=80,
                             timeout_s=launch.timeout_for(80), permission_mode=grants.mode_for(model),
                             disallowed_tools=launch.WRITE_TOOLS, parts=True, judging=True,
                             allowed_tools=("Bash(git:*)", "Bash(python3:*)", "Bash(make:*)"))
    began = time.monotonic()
    r = launch.run(spec)
    parts = list(getattr(r, "parts", ()) or ())
    return {"cell": cell, "wall_s": round(time.monotonic() - began, 1), "turns": r.turns, "cost": round(r.cost_usd, 4),
            "passed": r.passed, "failure": r.failure.value if r.failure else None, "parts": len(parts),
            "surfaces": [p.get("surfaces") for p in parts]}


def main(argv: list[str]) -> None:
    out = HERE / "results-plan.jsonl"
    if argv[0] == "report":
        rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
        for cell in sorted({r["cell"] for r in rows}):
            rs = [r for r in rows if r["cell"] == cell]
            print(f"{cell:<12} n {len(rs)}  valid {sum(r['passed'] for r in rs)}/{len(rs)}  wall p50 "
                  f"{median(r['wall_s'] for r in rs):.0f}s  turns p50 {median(r['turns'] for r in rs)}  "
                  f"$ p50 {median(r['cost'] for r in rs):.2f}  parts {[r['parts'] for r in rs]}")
        return
    cells, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    jobs = [c for _ in range(reps) for c in cells]

    def run(cell: str) -> None:
        row = one(cell)
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 3) as pool:
        list(pool.map(run, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
