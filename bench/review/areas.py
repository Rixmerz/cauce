"""A large change reviewed whole, or by area side by side (`workflows._areas`, `_area_brief`)?

The `volume` change (1,221 diff lines, 69 files, 14 planted bugs). The `areas` arm runs one blind
opus/high review per top-level area at once, each with locks' own area brief, and adds their
findings up; its path is the slowest area's. The whole review is `harness.py`'s `single` arm.

    python bench/review/areas.py run 3
    python bench/review/analyze.py bench/review/results-areas.jsonl
"""
from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import harness  # noqa: E402

from locks import isolate, ledger, workflows  # noqa: E402


def areas(where: Path) -> list[dict]:
    base = subprocess.run(["git", "rev-parse", "main"], cwd=where, capture_output=True, text=True).stdout.strip()
    changed = list(isolate.numstat(where, base, "HEAD") or ())
    groups: dict[str, dict] = {}
    for _, path in changed:
        pid, spec = (path.split("/", 1)[0], [path.split("/", 1)[0] + "/"]) if "/" in path else ("top", None)
        groups.setdefault(pid, {"id": pid, "paths": [], "spec": spec})["paths"].append(path)
    lines, every = sum(n for n, _ in changed), [p for _, p in changed]
    found = [{**g, "spec": g["spec"] or g["paths"], "lines": lines, "others": [p for p in every if p not in g["paths"]]}
             for g in groups.values()]
    for n, g in enumerate(found, 1):
        g["n"], g["of"] = n, len(found)
    return found


def chain() -> dict:
    where = harness.repository("volume")
    base = subprocess.run(["git", "rev-parse", "main"], cwd=where, capture_output=True, text=True).stdout.strip()
    parts = areas(where)
    try:
        with ThreadPoolExecutor(len(parts)) as pool:
            done = list(pool.map(lambda a: harness.worker(
                where, harness.BLIND.format(target=harness.TARGET) + "\n\n" + workflows._area_brief(a, base),
                "opus/high"), parts))
    finally:
        import shutil
        shutil.rmtree(where.parent, ignore_errors=True)
    steps = {a["id"]: d for a, d in zip(parts, done, strict=True)}
    rows = ledger.collect([(k, v["findings"]) for k, v in steps.items()])
    return {"arm": "areas", "variant": "volume", "rows": rows, "confirmed": [r["id"] for r in rows],
            "steps": steps, "path_s": max(d["wall_s"] for d in done), "cost": round(sum(d["cost"] for d in done), 4)}


def main(argv: list[str]) -> None:
    out = HERE / "results-areas.jsonl"
    for rep in range(int(argv[1]) if len(argv) > 1 else 3):
        row = {"run": f"areas-{rep}", **chain()}
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps({k: row[k] for k in ("run", "cost", "path_s")} | {"areas": list(row["steps"])}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
