"""What each arm's confirmed rows caught of the planted bugs, at what cost and wall time.

    python bench/review/analyze.py bench/review/results.jsonl [--rows]
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent


def bugs(variant: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(HERE / "truth.tgz") as tar:
            tar.extractall(tmp, filter="data")
        scope: dict = {}
        exec((Path(tmp) / "truth.py").read_text(), scope)  # noqa: S102 - the bench's own file
    found = [b for b in scope["BUGS"] if variant == "volume" or not b.get("volume")]
    sys.path.insert(0, str(HERE))
    import harness
    change = harness.repository(variant)
    try:
        for b in found:  # the planted lines' numbers, read from the change
            b["lines"] = {}
            for f in b["files"]:
                text = (change / f).read_text().splitlines() if (change / f).exists() else []
                b["lines"][f] = [n for n, ln in enumerate(text, 1) if any(a in ln for a in b["at"])]
    finally:
        shutil.rmtree(change.parent, ignore_errors=True)
    return found


def where(row: dict) -> tuple[str, int | None]:
    m = re.match(r"\s*([^\s:#]+)(?:[:#]L?(\d+))?", row.get("where") or "")
    return (m.group(1).lstrip("./"), int(m.group(2)) if m and m.group(2) else None) if m else ("", None)


def match(row: dict, planted: list[dict]) -> str | None:
    """The planted bug a row is about: its file, then its line or its words."""
    path, line = where(row)
    text = f"{row.get('what', '')} {row.get('proof', '')}".lower()
    best = None
    for b in planted:
        files = [f for f in b["files"] if path.endswith(f) or (path and f.endswith(path))]
        if not files and not any(f.rsplit("/", 1)[-1] in text for f in b["files"]):
            continue
        near = line is not None and any(abs(line - n) <= 2 for f in files for n in b["lines"].get(f, []))
        exact = line is not None and any(line == n for f in files for n in b["lines"].get(f, []))
        said = any(w in text for w in b["words"])
        score = 2 * said + near if said or exact else 0
        if score and (best is None or score > best[0]):
            best = (score, b["id"])
    return best[1] if best else None


def main(argv: list[str]) -> None:
    rows = [json.loads(x) for x in Path(argv[0]).read_text().splitlines() if x.strip()]
    planted = bugs(rows[0].get("variant", "plain"))
    n_bugs = len(planted)
    arms = defaultdict(list)
    for r in rows:
        arms[r["arm"]].append(r)
    print(f"{'arm':<8}{'n':>3}{'caught':>9}{'recall':>8}{'other':>7}{'refuted ok':>11}{'cost p50':>10}"
          f"{'cost/bug':>10}{'path p50':>10}  per bug")
    for arm, runs in sorted(arms.items()):
        caught, other, seen_by_bug = [], [], defaultdict(int)
        for r in runs:
            ids = set(r["confirmed"])
            got = {match(row, planted) for row in r["rows"] if row["id"] in ids} - {None}
            caught.append(len(got))
            other.append(sum(1 for row in r["rows"] if row["id"] in ids and not match(row, planted)))
            for b in got:
                seen_by_bug[b] += 1
        lost = sum(1 for r in runs for row in r["rows"]
                   if row["id"] not in r["confirmed"] and match(row, planted))
        total = sum(r["cost"] for r in runs)
        whole = n_bugs * len(runs)
        print(f"{arm:<8}{len(runs):>3}{sum(caught):>6}/{whole:<3}{sum(caught) / whole:>7.0%}"
              f"{sum(other):>7}{lost:>11}{median(r['cost'] for r in runs):>10.3f}"
              f"{total / max(1, sum(caught)):>10.3f}{median(r['path_s'] for r in runs):>10.0f}  "
              + " ".join(f"{b['id']}:{seen_by_bug[b['id']]}" for b in planted))
    if "--rows" in argv:
        for r in rows:
            print(f"\n== {r['run']}")
            for row in r["rows"]:
                mark = "+" if row["id"] in r["confirmed"] else "-"
                print(f" {mark} {row['id']:<4}{match(row, planted) or '--':<4}{row['where'][:40]:<42}"
                      f"{row['what'][:110]}")


if __name__ == "__main__":
    main(sys.argv[1:])
