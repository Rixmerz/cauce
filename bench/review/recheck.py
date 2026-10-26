"""After a fix: the fix's independent verifier and then the re-check (two readers of one diff,
one after the other), the re-check alone, or one reader that does both.

The fix answers the nine rows of the `plain` change. It fixes seven. It "fixes" one by changing
the test data instead of the code (U), and one only in the docs (N). It drops a rule from the
config and changes a test's expected total to hide that (R).

    python bench/review/recheck.py run both,recheck,merged 3 3
    python bench/review/recheck.py run verifier:sonnet/high,verifier:sonnet/medium 3 3
    python bench/review/recheck.py report
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import harness  # noqa: E402

from cauce import ledger  # noqa: E402
from cauce.orchestrate import verifier_brief  # noqa: E402

ROWS = [("src/tally/money.py:18", "split drops the remainder: its shares do not add up to the amount"),
        ("src/tally/rules/loyalty.py:19", "loyalty takes its percent with the legacy `percent`, not the house `pct`"),
        ("src/tally/rules/loyalty.py:16", "the tier lookup is case-sensitive; the customers' tiers are GOLD, SILVER"),
        ("src/tally/rules/promo.py:16", "promo matches a substring of the raw tags string, not the tag"),
        ("src/tally/rules/bulk.py:14", "bulk takes `> 10`: a line of exactly 10 gets nothing"),
        ("src/tally/rules.cfg:1", "bulk is registered but not listed in rules.cfg, so it never runs"),
        ("src/tally/receipt.py:12", "negative amounts print wrong: a -1.50 discount shows -2.50"),
        ("src/tally/checkout.py:36", "order rules get the running total, not the subtotal: they stack"),
        ("src/tally/export.py:13", "the CSV is not quoted: an item name with a comma shifts the columns")]
LEDGER = [{"id": f"F{n}", "where": w, "what": what, "evidence_class": "deterministic", "cause": "introduced",
           "proof": "", "seen_by": ["review"]} for n, (w, what) in enumerate(ROWS, 1)]
FIX_ASK = f"Fix, in: {harness.TARGET}, the confirmed findings below.\n\n" + ledger.brief(
    "fix", LEDGER, round_=1, sha=ledger.digest(LEDGER))
MORE = ("\n\nBeyond these rows, check the whole fix for anything it broke or hid: behaviour it changed that no "
        "row asked for, a test or test data changed to pass, a row written down instead of fixed. Report each "
        "in `findings`, with its place, its proof and whether the fix caused it.")


def repository() -> tuple[Path, str]:
    where = harness.repository("plain")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=where, capture_output=True, text=True).stdout.strip()
    shutil.copytree(HERE / "fix", where, dirs_exist_ok=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "Fix the review's rows"],
                   cwd=where, check=True)
    return where, base


def recheck_ask(base: str, more: bool) -> str:
    return (f"Check, without changing any file, the fix just made in: {harness.TARGET}\n\n"
            + ledger.brief("recheck", LEDGER, round_=1, sha=ledger.digest(LEDGER), diff=f"git diff {base[:12]} HEAD")
            + (MORE if more else ""))


def chain(arm: str) -> dict:
    where, base = repository()
    steps: dict[str, dict] = {}
    ids = tuple(r["id"] for r in LEDGER)
    words = ledger.WORDS["recheck"]
    try:
        if arm == "both":
            steps["verifier"] = harness.worker(where, verifier_brief(FIX_ASK, "implement", where, base,
                                                                     _changed(where, base)), "sonnet/high")
        if arm.startswith("verifier:"):  # the fix's verifier alone, at the cell named
            steps["verifier"] = harness.worker(where, verifier_brief(FIX_ASK, "implement", where, base,
                                                                     _changed(where, base)), arm.split(":", 1)[1])
        if arm in ("both", "recheck"):
            steps["recheck"] = harness.worker(where, recheck_ask(base, False), "opus/high", ids, words)
        if arm == "merged":
            steps["merged"] = harness.worker(where, recheck_ask(base, True), "opus/high", ids, words)
    finally:
        shutil.rmtree(where.parent, ignore_errors=True)
    return {"arm": arm, "steps": steps, "cost": round(sum(s["cost"] for s in steps.values()), 4),
            "path_s": round(sum(s["wall_s"] for s in steps.values()), 1)}


def _changed(where: Path, base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], cwd=where, capture_output=True, text=True)
    return out.stdout.split()


def caught(row: dict) -> dict[str, bool]:
    """What a chain caught: the row fixed in the test data (U), the row only written down (N),
    the rule the fix dropped (R), and the fixed rows it called open (false)."""
    said = {v["id"]: v["verdict"] for s in row["steps"].values() for v in s.get("verdicts", [])}
    text = " ".join(f"{f.get('where', '')} {f.get('what', '')} {f.get('proof', '')}".lower()
                    for s in row["steps"].values() for f in s["findings"])
    text += " " + " ".join(v.get("proof", "").lower() for s in row["steps"].values() for v in s.get("verdicts", []))
    open_ = {i for i, v in said.items() if v in ("not_fixed", "regression")}
    return {"U": "F3" in open_ or "fixture" in text or "customers.json" in text,
            "N": "F9" in open_ or any(w in text for w in ("quot", "csv module", "csv.writer", "written down")),
            "R": "staff" in text,
            "false": len(open_ - {"F3", "F9"})}


def main(argv: list[str]) -> None:
    out = HERE / "results-recheck.jsonl"
    if argv[0] == "report":
        rows = [json.loads(x) for x in out.read_text().splitlines() if x.strip()]
        print(f"{'arm':<24}{'n':>3}{'U':>4}{'N':>4}{'R':>4}{'false':>7}{'cost p50':>10}{'path p50':>10}")
        for arm in sorted({r["arm"] for r in rows}):
            rs = [r for r in rows if r["arm"] == arm]
            got = [caught(r) for r in rs]
            print(f"{arm:<24}{len(rs):>3}" + "".join(f"{sum(g[k] for g in got):>4}" for k in "UNR")
                  + f"{sum(g['false'] for g in got):>7}{median(r['cost'] for r in rs):>10.3f}"
                  f"{median(r['path_s'] for r in rs):>10.0f}")
        return
    arms, reps = argv[1].split(","), int(argv[2]) if len(argv) > 2 else 3
    done = {json.loads(x)["run"] for x in out.read_text().splitlines()} if out.exists() else set()
    jobs = [f"{a}-{n}" for n in range(reps) for a in arms if f"{a}-{n}" not in done]

    def one(run: str) -> None:
        began = time.monotonic()
        row = {"run": run, **chain(run.rsplit("-", 1)[0])}
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps({"run": run, "cost": row["cost"], "path_s": row["path_s"], **caught(row),
                          "took": round(time.monotonic() - began)}), flush=True)

    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 3) as pool:
        list(pool.map(one, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
