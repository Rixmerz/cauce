"""Can a cheaper cell do `refactor` and `test` work as right as the ladder's first rung?

Real: one `claude -p` worker per run, started by cauce's `launch.run`, on a fresh copy of the
pricing engine `bench/memory/hard`. Each kind has one request and a judge the worker never sees:

- `refactor`: rename the rule methods and move each rule module by its stage, behaviour kept.
  Judged by checks of the new layout and by pricing a set of carts the same as the untouched
  engine does (`REFACTOR_CARTS`): right only when every check holds.
- `test`: write tests for the money and line helpers. Judged by mutation: each of `MUTANTS` is
  applied to the source in turn and the worker's tests are run against it; a mutant the tests
  fail on is killed. Right only when the tests pass on the untouched code and kill every mutant.

The arms are cells (`haiku/high`, `sonnet/medium`, ...). What it measured is in the README.

    python bench/speed/kinds.py run refactor,test haiku/high,sonnet/medium 3 4
    python bench/speed/kinds.py report
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from cauce import grants, launch  # noqa: E402
from cauce.matrix import Cell  # noqa: E402

BASE = HERE.parent / "memory" / "hard"
OUT = HERE / "results-kinds.jsonl"
MAX_TURNS = 80
ASKS = {
    "refactor": ("Refactor the pricing rules without changing what any of them charges: rename the rule methods "
                 "everywhere, `apply_line` to `on_line` and `apply_order` to `on_order` (the base class, every rule, "
                 "the checkout and the tests), and move each rule module into `src/tally/rules/line/` or "
                 "`src/tally/rules/order/` by its stage, keeping `tally.rules.RULES` as it is. Run the suite before "
                 "you stop."),
    "test": ("Write tests for `tally.money` (`pct`, `percent`, `fmt`) and `tally.lines` (`tags_of`, `amount`) in "
             "`tests/`: cover how each rounds, odd and empty inputs, and what each returns. Change no source file. "
             "Run the suite before you stop."),
}
#: Carts priced before and after a refactor: the same totals, or the refactor changed behaviour.
REFACTOR_CARTS = [
    ([("a", 250, 2, "")], "cy"),
    ([("a", 1000, 1, "Clearance"), ("b", 500, 1, "")], "ben"),
    ([("a", 999, 3, " clearance , sale"), ("b", 1, 7, "CLEARANCE")], "ben"),
    ([("a", 333, 1, "sale")], "cy"),
]
#: Mutants of the helpers a `test` worker's tests must catch: (file, before, after).
MUTANTS = [
    ("src/tally/money.py", "ROUND_HALF_UP)", "ROUND_HALF_EVEN)"),
    ("src/tally/money.py", "return round(amount_cents * rate)", "return int(amount_cents * rate)"),
    ("src/tally/money.py", ":02d}", ":d}"),
    ("src/tally/money.py", "amount_cents // 100", "amount_cents // 10"),
    ("src/tally/lines.py", "t.strip().lower()", "t.lower()"),
    ("src/tally/lines.py", ".lower() for", " for"),
    ("src/tally/lines.py", 'return line["price"] * line["qty"]', 'return line["price"]'),
    ("src/tally/lines.py", "if t.strip()}", "}"),
]


def repository() -> Path:
    where = Path(tempfile.mkdtemp(prefix="tally-kinds-")) / "repo"
    shutil.copytree(BASE, where)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init"], cwd=where,
                   check=True)
    return where


PRICES = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tally.checkout import price
customers = json.loads((Path(sys.argv[2]) / "tests" / "fixtures" / "customers.json").read_text())
carts = json.loads(sys.argv[3])
print(json.dumps([price({"lines": [dict(name=n, price=p, qty=q, tags=t) for n, p, q, t in lines]}, customers[c])
                  for lines, c in carts]))
"""

LAYOUT = """
import importlib, inspect, sys
sys.path.insert(0, sys.argv[1])
out = {}
from tally.rules import RULES
out["rules kept"] = sorted(RULES) == ["clearance", "staff"]
try:
    importlib.import_module("tally.rules.line.clearance"); importlib.import_module("tally.rules.order.staff")
    out["modules moved"] = True
except Exception:
    out["modules moved"] = False
from tally.rules.base import Rule
out["base renamed"] = hasattr(Rule, "on_line") and hasattr(Rule, "on_order") and not hasattr(Rule, "apply_line") \\
    and not hasattr(Rule, "apply_order")
out["rules renamed"] = all(not hasattr(type(r), "apply_line") and not hasattr(type(r), "apply_order")
                           for r in RULES.values())
import json; print(json.dumps(out))
"""


def _py(code: str, *args: str) -> str:
    done = subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, timeout=120)
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""


def judge_refactor(where: Path) -> dict:
    carts = json.dumps(REFACTOR_CARTS)
    before = _py(PRICES, str(BASE / "src"), str(BASE), carts)
    try:
        after = _py(PRICES, str(where / "src"), str(where), carts)
        checks = json.loads(_py(LAYOUT, str(where / "src")) or "{}")
    except (subprocess.TimeoutExpired, ValueError):
        after, checks = "", {}
    checks["prices kept"] = bool(after) and after == before
    held = sum(bool(v) for v in checks.values())
    return {"held": held, "of": 5, "right": held == 5, "checks": checks}


def _tests_pass(where: Path) -> bool:
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=where,
                          capture_output=True, text=True, timeout=300, env={"PYTHONPATH": str(where / "src"),
                                                                          "PATH": "/usr/bin:/bin"})
    return done.returncode == 0


def judge_test(where: Path) -> dict:
    source_kept = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src"], cwd=where).returncode == 0
    green = _tests_pass(where)
    killed = 0
    for path, old, new in MUTANTS:
        target = where / path
        text = target.read_text()
        if old not in text:
            continue
        target.write_text(text.replace(old, new, 1))
        try:
            killed += not _tests_pass(where)
        finally:
            target.write_text(text)
    return {"held": killed, "of": len(MUTANTS), "green": green, "source_kept": source_kept,
            "right": green and source_kept and killed == len(MUTANTS)}


def one(kind: str, cell: str) -> dict:
    model, effort = cell.split("/")
    where = repository()
    spec = launch.LaunchSpec(prompt=ASKS[kind], cell=Cell(model, effort), launch_dir=where, max_turns=MAX_TURNS,
                             timeout_s=launch.timeout_for(MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)"))
    began = time.monotonic()
    try:
        r = launch.run(spec)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        shutil.rmtree(where.parent, ignore_errors=True)
        return {"kind": kind, "cell": cell, "error": str(exc)[:300]}
    wall = time.monotonic() - began
    judged = judge_refactor(where) if kind == "refactor" else judge_test(where)
    row = {"kind": kind, "cell": cell, "wall_s": round(wall, 1), "turns": r.turns, "cost": round(r.cost_usd, 4),
           "claimed": r.passed, **judged}
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def report() -> None:
    rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip() and "error" not in x]
    print(f"{'kind':<10}{'cell':<15}{'n':>3}{'right':>8}{'held':>9}{'wall p50':>10}{'turns':>7}{'$ p50':>8}"
          f"{'$ per right':>13}")
    for kind, cell in sorted({(r["kind"], r["cell"]) for r in rows}):
        rs = [r for r in rows if r["kind"] == kind and r["cell"] == cell]
        right = sum(r["right"] for r in rs)
        spent = sum(r["cost"] for r in rs)
        print(f"{kind:<10}{cell:<15}{len(rs):>3}{right:>5}/{len(rs):<2}"
              f"{sum(r['held'] for r in rs):>5}/{sum(r['of'] for r in rs):<3}"
              f"{median(r['wall_s'] for r in rs):>10.0f}{median(r['turns'] for r in rs):>7}"
              f"{median(r['cost'] for r in rs):>8.3f}{(spent / right if right else float('inf')):>13.3f}")


def main(argv: list[str]) -> None:
    if argv[0] == "report":
        report()
        return
    kinds, cells = argv[1].split(","), argv[2].split(",")
    reps = int(argv[3]) if len(argv) > 3 else 3
    jobs = [(k, c) for _ in range(reps) for k in kinds for c in cells]

    def go(job: tuple[str, str]) -> None:
        row = one(*job)
        with OUT.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    with ThreadPoolExecutor(int(argv[4]) if len(argv) > 4 else 4) as pool:
        list(pool.map(go, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
