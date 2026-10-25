"""The benchmark's arms side by side: hidden-test passes, silent failures, cost and turns."""
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

for path in sys.argv[1:]:
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    rows = [r for r in rows if not r["run"].startswith("teach-") and "error" not in r]
    arms = defaultdict(list)
    for r in rows:
        arm = r["run"].split("-", 1)[1].rsplit("-", 1)[0]  # model-memory
        arms[arm].append(r)
    print(f"\n== {path}: {len(rows)} runs")
    print(f"{'arm':<16}{'n':>3}{'hidden':>8}{'silent':>8}{'claimed':>8}{'cost p50':>10}{'cost tot':>10}"
          f"{'turns p50':>10}{'wall p50':>10}{'$/success':>11}")
    for arm in sorted(arms):
        rs = arms[arm]
        ok = sum(r["hidden"] for r in rs)
        silent = sum(r["visible"] and r["claimed"] and not r["hidden"] for r in rs)
        tot = sum(r["cost"] for r in rs)
        print(f"{arm:<16}{len(rs):>3}{ok:>5}/{len(rs):<2}{silent:>8}{sum(r['claimed'] for r in rs):>8}"
              f"{median(r['cost'] for r in rs):>10.4f}{tot:>10.3f}{median(r['turns'] for r in rs):>10}"
              f"{median(r['wall_s'] for r in rs):>10.1f}{(tot / ok if ok else float('inf')):>11.4f}")
    fails = [r for r in rows if not r["hidden"]]
    for r in fails:
        print("  fail:", r["run"], "claimed" if r["claimed"] else r["failure"], r["hidden_tail"])
