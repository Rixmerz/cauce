"""Does cauce's own check overlap the independent verifier without either slowing the other?

Real: a git worktree of cauce at a real commit, cauce's full suite as the check, and a real
verifier (cauce's `verifier_brief`, its cell, read-only) on that commit's change. Each rep runs
them one after the other (as cauce does today) and side by side, and keeps both walls and both
outcomes.

    python bench/speed/overlap.py <commit> [reps]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE.parent / "review"))

import harness  # noqa: E402

from cauce.orchestrate import verifier_brief  # noqa: E402

CHECK = ".venv/bin/python -m pytest -q -p no:cacheprovider"


def worktree(commit: str) -> Path:
    where = Path(tempfile.mkdtemp(prefix="cauce-bench-")) / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(where), commit], cwd=ROOT, check=True)
    (where / ".venv").symlink_to(ROOT / ".venv")
    return where


def check(where: Path) -> dict:
    began = time.monotonic()
    done = subprocess.run(CHECK, shell=True, cwd=where, capture_output=True, text=True, timeout=1800)
    return {"code": done.returncode, "wall_s": round(time.monotonic() - began, 1),
            "tail": done.stdout.strip().splitlines()[-1:]}


def verify(where: Path, commit: str) -> dict:
    subject = subprocess.run(["git", "log", "-1", "--format=%B", commit], cwd=ROOT, capture_output=True,
                             text=True).stdout.strip()
    changed = subprocess.run(["git", "diff", "--name-only", f"{commit}~1", commit], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    prompt = verifier_brief(subject, "implement", where, f"{commit}~1", changed)
    out = harness.worker(where, prompt, "opus/high")
    return {k: out[k] for k in ("passed", "cost", "wall_s", "turns")} | {"findings": len(out["findings"])}


def main(argv: list[str]) -> None:
    commit, reps = argv[0], int(argv[1]) if len(argv) > 1 else 3
    out = HERE / "results-overlap.jsonl"
    for rep in range(reps):
        for mode in ("serial", "parallel"):
            where = worktree(commit)
            began = time.monotonic()
            try:
                if mode == "serial":
                    c = check(where)
                    v = verify(where, commit)
                else:
                    with ThreadPoolExecutor(2) as pool:
                        fc, fv = pool.submit(check, where), pool.submit(verify, where, commit)
                        c, v = fc.result(), fv.result()
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", str(where)], cwd=ROOT)
            row = {"rep": rep, "mode": mode, "wall_s": round(time.monotonic() - began, 1), "check": c, "verifier": v}
            with out.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
