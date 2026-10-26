"""How much effort does one blind review of a large change need?

The `volume` change (1,221 diff lines, 14 planted bugs), one blind review per run, the same
prompt as `harness.py`'s `single` arm, at each cell given. Findings are kept as they come (no
refuter), so recall is the review's own.

    python bench/review/effort.py run opus/medium,opus/high,sonnet/xhigh 3 3
    python bench/review/analyze.py bench/review/results-effort.jsonl
"""
from __future__ import annotations

import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import harness  # noqa: E402

from cauce import ledger  # noqa: E402


def one(cell: str) -> dict:
    where = harness.repository("volume")
    try:
        done = harness.worker(where, harness.ALONE.format(target=harness.TARGET), cell)
    finally:
        shutil.rmtree(where.parent, ignore_errors=True)
    rows = ledger.collect([("review", done["findings"])])
    return {"arm": cell, "variant": "volume", "rows": rows, "confirmed": [r["id"] for r in rows],
            "steps": {"review": done}, "path_s": done["wall_s"], "cost": done["cost"]}


def main(argv: list[str]) -> None:
    out = HERE / "results-effort.jsonl"
    cells, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3

    def run(job: tuple[int, str]) -> None:
        rep, cell = job
        row = {"run": f"{cell}-{rep}", **one(cell)}
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps({k: row[k] for k in ("run", "cost", "path_s")} | {"rows": len(row["rows"])}), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 3) as pool:
        list(pool.map(run, [(r, c) for r in range(reps) for c in cells]))


if __name__ == "__main__":
    main(sys.argv[1:])
