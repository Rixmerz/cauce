"""Real cauce runs whose every worker goes through `tap.py`: what each was sent and what it read.

Each run is `cauce run` on a fresh copy of `bench/memory/hard`, checked by its own tests and the
`lore` variant's CI (which holds a policy no file states), so some first tries fail and the briefs
after them carry what failed. Four arms: `memory` (notes, a constraint and a dead end kept for the
project before it starts, as cauce keeps them), `cold` (nothing kept), `hidden` (cold, its CI behind
a script that unpacks it only while it runs, so first tries fail and later briefs carry what
failed), and `review` (the bundled `review-fix` workflow on a commit with planted bugs: a ledger,
refute, fix and recheck briefs). Real money.

    python bench/tokens/collect.py <reps> [parallel] [arm,arm]   # writes bench/tokens/tap-*.jsonl
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MEMORY = ROOT / "bench" / "memory"
sys.path.insert(0, str(MEMORY))

from harness import SUFFIX, TASKS  # noqa: E402

NOTES = [
    ("Money is an int of cents. Percentages go through money.pct (Decimal, ROUND_HALF_UP); money.percent uses "
     "Python's round (half to even) and is legacy: only rules/staff.py still calls it.", ["src/tally/money.py"]),
    ("A rule runs only when it is listed in src/tally/rules.cfg and registered in RULES in "
     "src/tally/rules/__init__.py; one missing from either never runs.",
     ["src/tally/rules.cfg", "src/tally/rules/__init__.py"]),
    ("A customer's loyalty is nested: customer['loyalty'] is None or {'tier': 'GOLD'|'SILVER' in upper case, "
     "'since': 'YYYY-MM-DD'}. tests/fixtures/customers.json holds the real shapes.",
     ["tests/fixtures/customers.json"]),
    ("A line's tags arrive as one comma-separated string whose case varies ('promo,GiftCard'): split it and "
     "compare lower-cased.", ["src/tally/lines.py"]),
    ("Order rules see the subtotal after every line rule. A rule returns Adjustments and never mutates the cart "
     "or the line: every rule is given the same objects.", ["src/tally/rules/base.py"]),
]
CONSTRAINT = ("POL-7 (finance policy, enforced by CI): gift card lines (tag giftcard) are never discounted, by a "
              "line rule or as part of the subtotal an order rule takes its percentage of.")
#: A loyalty rule with the bugs `hard` hides: banker's rounding, a mutated cart, a lower-case tier.
PLANTED = '''from tally.money import percent
from tally.rules.base import Adjustment, Rule


class Loyalty(Rule):
    name = "loyalty"
    stage = "order"

    def apply_order(self, subtotal_cents, cart, customer):
        tier = (customer.get("loyalty") or {}).get("tier")
        if tier == "gold":
            cart["discounted"] = True
            return [Adjustment("loyalty", -percent(subtotal_cents, 0.1))]
        return []
'''
CI_SCRIPT = '''#!/bin/sh
# The project's CI suite: unpacked only while it runs.
set -e
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT
tar xzf "{tgz}" -C "$d"
rc=0
PYTHONPATH="src:$d/ci" python3 -m pytest -q -p no:cacheprovider "$d/ci/h$1.py" > "$d/out" 2>&1 || rc=$?
sed "s#$d#ci-root#g" "$d/out"
exit $rc
'''
DEAD_END = {"problem": "a loyalty discount off by one cent against the expected total",
            "fix": "computed the discount with money.percent(subtotal, rate)",
            "why": "money.percent rounds half to even; the house rounds half up (money.pct)"}


def env(home: Path, log: Path, shim: Path) -> dict[str, str]:
    return {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "TAP_LOG": str(log), "CAUCE_HOME": str(home),
            "CAUCE_LIVESPEC": "off", "CAUCE_ENGRAM": "off", "CAUCE_NAMES": "off", "CAUCE_AUTOWORK": "off",
            "CAUCE_HABIT_REVIEW": "off", "CAUCE_HABIT_AUTO": "off", "CAUCE_ROLE_TRIALS": "off",
            "CAUCE_PARALLEL": "off", "CAUCE_LATE_VERIFIER": "off", "CAUCE_LIVESPEC_WAIT": "0"}


def cauce(args: list[str], where: Path, e: dict[str, str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run([str(ROOT / "bin" / "cauce"), *args], cwd=where, env=e, capture_output=True, text=True,
                          timeout=timeout, check=False)


def one(job: tuple[str, str, str], home: Path, log: Path, shim: Path, ci: Path, ci_script: Path) -> dict:
    run_id, task, arm = job
    where = Path(tempfile.mkdtemp(prefix="tally-")) / "repo"
    shutil.copytree(MEMORY / "hard", where)
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                 ["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init"]):
        subprocess.run(argv, cwd=where, check=True)
    e = {**env(home, log, shim), "TAP_RUN": run_id}
    if arm == "memory":
        for text, anchors in NOTES:
            cauce(["note", text, "--topic", "code", *[a for p in anchors for a in ("--anchor", p)]], where, e)
        cauce(["note", CONSTRAINT, "--topic", "business", "--constraint"], where, e)
        cauce(["memory", "record", "--problem", DEAD_END["problem"], "--fix", DEAD_END["fix"], "--outcome",
               "failed", "--why", DEAD_END["why"]], where, e)
    verify = (f"make test && PYTHONPATH=src:{ci} python3 -m pytest -q -p no:cacheprovider {ci}/h{task[1]}.py")
    if arm == "hidden":
        verify = f"make test && sh {ci_script} {task[1]}"
    if arm == "review":
        (where / "src/tally/rules/loyalty.py").write_text(PLANTED)
        init = where / "src/tally/rules/__init__.py"
        init.write_text("from tally.rules.clearance import Clearance\nfrom tally.rules.loyalty import Loyalty\n"
                        "from tally.rules.staff import Staff\n\nRULES = {rule.name: rule for rule in (Clearance(), "
                        "Loyalty(), Staff())}\n")
        (where / "src/tally/rules.cfg").write_text((where / "src/tally/rules.cfg").read_text() + "loyalty\n")
        subprocess.run(["git", "add", "-A"], cwd=where, check=True)
        subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm",
                        "Add a 10% loyalty discount for gold customers"], cwd=where, check=True)
        cauce(["workflow", "run", "review-fix", "the change in the last commit (`git show HEAD`): a 10% order "
               "discount for gold-tier loyalty customers", "--verify", "make test"], where, e)
        # The run's own dispatcher works it; wait until no run here is active.
        began = time.monotonic()
        while time.monotonic() - began < 5400:
            cauce(["work"], where, e, timeout=5400)
            runs = json.loads(cauce(["workflow", "status", "--json"], where, e).stdout or "[]")
            if runs and not any(r.get("state") in ("running", "queued") for r in runs):
                break
            time.sleep(20)
        done = cauce(["workflow", "status"], where, e)
    else:
        done = cauce(["run", TASKS["lore"][task] + SUFFIX, "--verify", verify, "--start", "haiku/medium",
                      "--max-attempts", "4"], where, e)
    tail = (done.stdout.strip().splitlines() or [""])[:3]
    shutil.rmtree(where.parent, ignore_errors=True)
    row = {"run": run_id, "task": task, "arm": arm, "exit": done.returncode, "report": tail}
    print(json.dumps(row), flush=True)
    return row


def main(argv: list[str]) -> None:
    reps, parallel = int(argv[0]), int(argv[1]) if len(argv) > 1 else 4
    arms = argv[2].split(",") if len(argv) > 2 else ["memory", "cold"]
    work = Path(tempfile.mkdtemp(prefix="cauce-tokens-"))
    home, shim, ci = work / "home", work / "bin", work / "ci-root"
    shim.mkdir()
    (shim / "claude").symlink_to(HERE / "tap.py")
    ci.mkdir()
    subprocess.run(["tar", "xzf", str(MEMORY / "hidden" / "lore.tgz"), "-C", str(ci)], check=True)
    ci_script = work / "ci.sh"
    ci_script.write_text(CI_SCRIPT.format(tgz=MEMORY / "hidden" / "lore.tgz"))
    log = HERE / f"tap-{work.name}.jsonl"
    jobs = [(f"{t}-{arm}-{r}", t, arm) for r in range(reps) for arm in arms
            for t in (("t1",) if arm == "review" else ("t1", "t2", "t3"))]
    with ThreadPoolExecutor(parallel) as pool:
        rows = list(pool.map(lambda j: one(j, home, log, shim, ci / "ci", ci_script), jobs))
    (HERE / f"runs-{work.name}.json").write_text(json.dumps({"log": log.name, "runs": rows}, indent=1))
    print(f"tap log: {log}")


if __name__ == "__main__":
    main(sys.argv[1:])
