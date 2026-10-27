"""Two blind reviewers with the same purpose, or a classifier before a quality check?

Real workers, so real money: each run is a review chain of `claude -p` workers launched with
cauce's own `launch.run`, on a fresh copy of a change to a small pricing engine with nine planted
bugs among renames, docs and tests. The chain's confirmed rows are matched to the planted bugs.

    python bench/review/harness.py pack                      # truth.py -> truth.tgz (gitignored source)
    python bench/review/harness.py run <plain|volume> <arm,...> [reps] [parallel]
    python bench/review/analyze.py bench/review/results-<variant>.jsonl

Arms, each a chain cauce runs or could run:
- `single`: one blind review, then a refuter on what it only read (`review-fix`).
- `dual`: two blind reviews with one purpose on two models, then a refuter on what only one saw
  and only read (`dual-review`).
- `split`: two blind reviews with different purposes: a prover that shows what it finds and
  sorts the change, beside a reviewer; then the refuter as in `dual`.
- `funnel`: a classifier sorts the change and shows what it can; a quality check, not blind,
  decides each of its rows, checks its classes and reviews what it left; no refuter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from cauce import grants, launch, ledger  # noqa: E402
from cauce.matrix import Cell  # noqa: E402

sys.path.insert(0, str(HERE))
import volume  # noqa: E402

BASE = HERE.parent / "memory" / "hard"
REQUEST = ("Add loyalty (gold 10%, silver 5% off the order), promo (lines tagged promo get 15% off) and bulk "
           "(a line of 10 or more of one item gets 5% off) rules, a printed receipt, and a CSV export of an "
           "order for finance.")
TARGET = f"the change on this branch, `git diff main...HEAD`. It was asked as: \"{REQUEST}\""
MAX_TURNS = int(os.environ.get("BENCH_MAX_TURNS", "0")) or 40
TOOLS = ("Bash(git:*)", "Bash(make:*)", "Bash(python3:*)")

BLIND = ("Review, without changing any file: {target}\n\nYou are one of two reviewers working apart: find the "
         "real problems on your own. Report each one in `findings`, with its place, its evidence and whether "
         "the change caused it. Say plainly when there is none.")
ALONE = ("Review, without changing any file: {target}\n\nBugs, missing tests, risky code. Report each real "
         "problem in `findings`: its place (file:line), what is wrong, how it is known (`deterministic` with the "
         "command or hunk that shows it as `proof`, else `inferential`) and whether the change caused it. Say "
         "plainly when there is none.")
PROVER = ("Classify, without changing any file: {target}\n\nYou are one of two reviewers working apart, and your "
          "part is to sort and to show, not to read every corner. (1) Put each file the change touches in one "
          "class, one line each in `summary`: `mechanical` (no behaviour changes: a rename, a move, formatting, "
          "comments, docs), `covered` (its behaviour changes and a test in the change would fail if it were "
          "wrong) or `uncovered` (its behaviour changes and no test would catch it). (2) Report in `findings` "
          "each problem you can show: by a command, a `python3 -c` run against the code, a test, or the hunk "
          "itself, as `deterministic` with that proof and whether the change caused it. Report one you could "
          "only read from the code as `inferential`.")
QUALITY = ("Review, without changing any file: {target}\n\nA classifier went through this change before you: it "
           "sorted its files and reported the rows below. You are the quality check and the last word before "
           "the fix. (1) Decide each row yourself, with your own proof: `confirmed`, `refuted` or `unsure`; "
           "never keep a row because it was reported. (2) Check its classes: open what it called `mechanical` "
           "or `covered` and see that it is; a hunk that changes behaviour is not mechanical, and a test that "
           "cannot fail covers nothing. (3) Review what it left `uncovered` and whatever its classes got wrong, "
           "and report in `findings` each problem it missed, with its place, its evidence and whether the change "
           "caused it.\n\nIts classes:\n{classes}\n\nIts rows:\n{rows}\n\nAnswer every row in `verdicts`, one "
           "entry per id, with `verdict` one of confirmed, refuted, unsure and the `proof` that shows it.")


def repository(variant: str) -> Path:
    where = Path(tempfile.mkdtemp(prefix="tally-")) / "repo"
    shutil.copytree(BASE, where)
    if variant == "volume":
        volume.base(where)
    git = ["git", "-c", "user.email=b@b", "-c", "user.name=b"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run([*git, "commit", "-qm", "Price carts by rules"], cwd=where, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "change"], cwd=where, check=True)
    if variant == "volume":
        volume.change(where, HERE / "change")
    else:
        shutil.copytree(HERE / "change", where, dirs_exist_ok=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run([*git, "commit", "-qm", "Loyalty, promo and bulk rules, a receipt and a CSV export"], cwd=where,
                   check=True)
    return where


def worker(where: Path, prompt: str, cell: str, ids: tuple[str, ...] = (),
           words: tuple[str, ...] = ledger.WORDS["refute"]) -> dict:
    model, effort = cell.split("/")
    spec = launch.LaunchSpec(prompt=prompt, cell=Cell(model, effort), launch_dir=where, max_turns=MAX_TURNS,
                             timeout_s=launch.timeout_for(MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=TOOLS, disallowed_tools=launch.WRITE_TOOLS, judging=True,
                             verdicts=ids, verdict_words=words if ids else ())
    began = time.monotonic()
    r = launch.run(spec)
    return {"cell": cell, "passed": r.passed, "failure": r.failure.value if r.failure else None,
            "cost": round(r.cost_usd, 4), "wall_s": round(time.monotonic() - began, 1), "turns": r.turns,
            "summary": r.summary, "findings": [dict(f) for f in r.findings], "verdicts": [dict(v) for v in r.verdicts]}


def refute(where: Path, rows: list[dict], reviewers: int, cell: str) -> tuple[list[str], dict | None]:
    """The refuter on the rows only one review saw and only read; the rest are confirmed as cauce does."""
    mine = [r for r in rows if ledger.disputed(r)]
    agreed = [r["id"] for r in rows if not ledger.disputed(r)]
    if not mine:
        return agreed, None
    said = worker(where, f"Decide, without changing any file, the findings on: {TARGET}\n\n"
                  + ledger.brief("refute", mine, round_=1, sha=ledger.digest(mine), reviewers=reviewers), cell,
                  tuple(r["id"] for r in mine))
    out = ledger.outcome({"role": "refute", "ids": [r["id"] for r in mine], "agreed": agreed}, said["verdicts"])
    return out.confirmed, said


def chain(arm: str, variant: str) -> dict:
    where = repository(variant)
    steps: dict[str, dict] = {}
    try:
        if arm == "single":
            steps["review"] = worker(where, ALONE.format(target=TARGET), "opus/high")
            rows = ledger.collect([("review", steps["review"]["findings"])])
            confirmed, said = refute(where, rows, 1, "opus/high")
            path = steps["review"]["wall_s"]
        elif arm in ("dual", "split"):
            first = PROVER if arm == "split" else BLIND
            with ThreadPoolExecutor(2) as pool:
                a = pool.submit(worker, where, first.format(target=TARGET), "sonnet/high")
                b = pool.submit(worker, where, BLIND.format(target=TARGET), "opus/high")
                steps["a"], steps["b"] = a.result(), b.result()
            rows = ledger.collect([("a", steps["a"]["findings"]), ("b", steps["b"]["findings"])])
            confirmed, said = refute(where, rows, 2, "opus/xhigh")
            path = max(steps["a"]["wall_s"], steps["b"]["wall_s"])
        else:  # funnel
            steps["classify"] = worker(where, PROVER.replace("You are one of two reviewers working apart, and your "
                                                              "part is", "You are the first of two: your part is")
                                       .format(target=TARGET), "sonnet/high")
            rows = ledger.collect([("classify", steps["classify"]["findings"])])
            qc = QUALITY.format(target=TARGET, classes=steps["classify"]["summary"] or "(none given)",
                                rows=ledger.rows_text(rows) or "(none)")
            steps["quality"] = worker(where, qc, "opus/high", tuple(r["id"] for r in rows))
            verdicts = {v["id"]: v["verdict"] for v in steps["quality"]["verdicts"]}
            kept = [r["id"] for r in rows if verdicts.get(r["id"]) == "confirmed"]
            added = ledger.collect([("quality", steps["quality"]["findings"])])
            for r in added:
                r["id"] = f"Q{r['id'][1:]}"
            rows = rows + added
            confirmed, said = kept + [r["id"] for r in added], None
            path = steps["classify"]["wall_s"] + steps["quality"]["wall_s"]
        if said:
            steps["refute"] = said
            path += said["wall_s"]
    finally:
        shutil.rmtree(where.parent, ignore_errors=True)
    return {"arm": arm, "variant": variant, "rows": rows, "confirmed": confirmed, "steps": steps,
            "path_s": round(path, 1), "cost": round(sum(s["cost"] for s in steps.values()), 4)}


def pack() -> None:
    with tarfile.open(HERE / "truth.tgz", "w:gz") as tar:
        tar.add(HERE / "truth.py", arcname="truth.py")


def main(argv: list[str]) -> None:
    if argv[0] == "pack":
        return pack()
    variant, arms, reps = argv[1], argv[2].split(","), int(argv[3]) if len(argv) > 3 else 3
    out = HERE / f"results-{variant}.jsonl"
    done = {json.loads(x)["run"] for x in out.read_text().splitlines()} if out.exists() else set()
    jobs = [f"{arm}-{rep}" for rep in range(reps) for arm in arms if f"{arm}-{rep}" not in done]

    def one(run: str) -> None:
        row = {"run": run, **chain(run.rsplit("-", 1)[0], variant)}
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(json.dumps({k: row[k] for k in ("run", "cost", "path_s")} | {"confirmed": len(row["confirmed"])}),
              flush=True)

    with ThreadPoolExecutor(int(argv[4]) if len(argv) > 4 else 4) as pool:
        list(pool.map(one, jobs))


if __name__ == "__main__":
    main(sys.argv[1:])
