"""Does memory written by an expensive model make a cheap one as good as it, for less?

Real workers, so real money: each run is one `claude -p` worker launched with locks' own
`launch.run` (its result contract, its worker environment, its lean flags), on a fresh copy
of a variant's repository in a temporary directory outside this one. Success is judged by
hidden tests the worker never sees; a run whose own check passed while the hidden tests
fail is a silent failure.

    python bench/memory/harness.py <easy|hard|lore> teachers
    python bench/memory/harness.py <easy|hard|lore> students [reps] [parallel]
    python bench/memory/harness.py <easy|hard|lore> brief      # the arms' memory, as briefed
    python bench/memory/analyze.py bench/memory/results-*.jsonl

`teachers` runs the seed task t0 once per model; its orientation and lessons are the memory
the students get. In `lore` the hidden suite is the project's CI and holds a policy nothing in
the repository states: a teacher CI rejects goes on in its own session with CI's output, as
locks' continuation does, and what it learned from that failure is kept as a constraint.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from locks import cells, grants, launch, notes  # noqa: E402
from locks.matrix import Cell  # noqa: E402

TASKS = {
    "easy": {
        "t0": "Add a pricing rule: customers whose tier is gold get 10% off the order.",
        "t1": "Add a pricing rule: each cart line with a quantity of 10 or more gets 5% off that line.",
        "t2": "Add a pricing rule: cart lines tagged promo get 15% off.",
        "t3": "Add a pricing rule: orders whose subtotal is 100.00 or more get 7.5% off.",
    },
    "hard": {
        "t0": "Add a pricing rule: gold-tier loyalty customers get 10% off the order.",
        "t1": "Add a pricing rule: silver-tier loyalty customers get 5% off the order.",
        "t2": "Add a pricing rule: cart lines tagged promo get 15% off.",
        "t3": "Add a pricing rule: customers who have been loyalty members since before 2022 get 2.5% off the "
              "order.",
    },
}
TASKS["lore"] = TASKS["hard"]
SUFFIX = "\n\nAdd tests for it, and run the suite before you stop."
CELLS = {"haiku": Cell("haiku", "medium"), "sonnet": Cell("sonnet", "medium"), "opus": Cell("opus", "high")}
MAX_TURNS = 40
CI_ROUNDS = 2
CI_SAYS = ("Your change passed its own check but the project's CI rejected it. CI's output is below. Find the "
           "cause, fix it, and run the suite again before you stop.")


def repository(variant: str) -> Path:
    return HERE / ("easy" if variant == "easy" else "hard")


def hidden(variant: str, task: str, where: Path) -> subprocess.CompletedProcess:
    """The hidden suite on the work in `where`. Its files exist only while it runs, outside the
    run's directory, and their path is never in what it prints: a worker must not read them."""
    with tempfile.TemporaryDirectory() as tmp:
        if variant == "lore":
            subprocess.run(["tar", "xzf", str(HERE / "hidden" / "lore.tgz"), "-C", tmp], check=True)
            suite = Path(tmp) / "ci"
        else:
            suite = Path(shutil.copytree(HERE / "hidden" / variant, Path(tmp) / "ci"))
        name = f"h{task[1]}.py" if variant != "easy" else f"{task}.py"
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"ci/{name}"],
                              cwd=tmp, capture_output=True, text=True, timeout=300,
                              env={"PYTHONPATH": f"{where / 'src'}:{suite}", "PATH": "/usr/bin:/bin"})
        return subprocess.CompletedProcess(done.args, done.returncode,
                                           done.stdout.replace(str(suite), "ci").replace(str(where), "."), "")


def memory(teacher: dict, *, framing: str = "constraints") -> str:
    """What locks briefs the next worker with, by its own functions: the teacher's orientation as
    the primer, its lessons as candidate notes, and (`constraints`) what CI's failure taught it as
    constraints; `notes` briefs every lesson as a note, as locks did before constraints."""
    parts = [cells.primer_text(teacher.get("orientation") or [])]
    facts = list(teacher.get("learned") or [])
    rules = list(teacher.get("constraints") or [])
    if framing == "notes":
        facts, rules = facts + rules, []
    found = [{"id": i + 1, "topic": "code", "title": notes.title_of(f, 80), "text": f, "state": notes.CANDIDATE,
              "anchors": [], "links": []} for i, f in enumerate(facts)]
    parts.append("\n".join(notes.brief_lines(found)))
    parts.append("\n".join(notes.constraint_lines(
        [{"id": 90 + i, "topic": "business", "title": notes.title_of(r, 80), "text": r, "state": notes.CANDIDATE,
          "kind": notes.CONSTRAINT, "anchors": [], "links": []} for i, r in enumerate(rules)])))
    return "\n\n".join(p for p in parts if p)


def run_one(variant: str, out: Path, run_id: str, task: str, model: str, brief: str = "",
            ci_rounds: int = 0) -> dict:
    where = Path(tempfile.mkdtemp(prefix="tally-")) / "repo"
    shutil.copytree(repository(variant), where)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=where, check=True)
    subprocess.run(["git", "add", "-A"], cwd=where, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-qm", "init"], cwd=where,
                   check=True)
    spec = launch.LaunchSpec(prompt=TASKS[variant][task] + SUFFIX + (f"\n\n{brief}" if brief else ""),
                             cell=CELLS[model], launch_dir=where, max_turns=MAX_TURNS,
                             timeout_s=launch.timeout_for(MAX_TURNS), permission_mode=grants.mode_for(model),
                             allowed_tools=("Bash(make:*)", "Bash(python3:*)", "Bash(git:*)"),
                             persist_session=bool(ci_rounds))
    began, cost, turns, rounds = time.monotonic(), 0.0, 0, 0
    try:
        result = launch.run(spec)
        cost, turns = result.cost_usd, result.turns
        checked = hidden(variant, task, where)
        while checked.returncode != 0 and rounds < ci_rounds and result.session_id:
            rounds += 1
            said = "\n".join(checked.stdout.strip().splitlines()[-40:])
            result = launch.run(dataclasses.replace(spec, resume_session=result.session_id, resume_prompt=CI_SAYS,
                                                    resume_note=said))
            cost, turns = cost + result.cost_usd, turns + result.turns
            checked = hidden(variant, task, where)
    except Exception as exc:  # a launch that broke is recorded, never retried here
        return {"run": run_id, "task": task, "model": model, "error": str(exc)[:300]}
    visible = subprocess.run(["make", "test"], cwd=where, capture_output=True, text=True, timeout=300)
    row = {"run": run_id, "task": task, "model": model, "claimed": result.passed,
           "failure": result.failure.value if result.failure else None, "visible": visible.returncode == 0,
           "hidden": checked.returncode == 0, "hidden_tail": checked.stdout.strip().splitlines()[-1:],
           "cost": round(cost, 4), "turns": turns, "ci_rounds": rounds, "wall_s": round(time.monotonic() - began, 1),
           "input": result.input_tokens, "cache_read": result.cache_read_tokens,
           "orientation": list(result.orientation), "learned": list(result.learned),
           "constraints": list(result.constraints) if rounds else []}
    with out.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps({k: row[k] for k in ("run", "claimed", "visible", "hidden", "cost", "turns")}), flush=True)
    shutil.rmtree(where.parent, ignore_errors=True)
    return row


def main(argv: list[str]) -> None:
    variant, phase = argv[0], argv[1]
    out = HERE / f"results-{variant}.jsonl"
    rows = [json.loads(x) for x in out.read_text().splitlines()] if out.exists() else []
    teachers = {r["model"]: r for r in rows if r["run"].startswith("teach-")}
    if phase == "teachers":
        with ThreadPoolExecutor(len(CELLS)) as pool:
            list(pool.map(lambda m: run_one(variant, out, f"teach-{m}", "t0", m,
                                            ci_rounds=CI_ROUNDS if variant == "lore" else 0), CELLS))
        return
    if phase == "brief":
        for model, teacher in teachers.items():
            print(f"== {model}\n{memory(teacher)}\n")
        return
    reps = int(argv[2]) if len(argv) > 2 else 2
    jobs = []
    for rep in range(reps):
        for task in ("t1", "t2", "t3"):
            jobs.append((f"{task}-opus-cold-{rep}", task, "opus", ""))
            for student in ("haiku", "sonnet"):
                jobs.append((f"{task}-{student}-cold-{rep}", task, student, ""))
                for source in ("opus", student):
                    for framing in (("notes", "constraints") if variant == "lore" else ("notes",)):
                        jobs.append((f"{task}-{student}-mem{'opus' if source == 'opus' else 'self'}"
                                     f"{'' if framing == 'notes' else '+rules'}-{rep}", task, student,
                                     memory(teachers[source], framing=framing)))
    done = {r["run"] for r in rows}
    with ThreadPoolExecutor(int(argv[3]) if len(argv) > 3 else 5) as pool:
        list(pool.map(lambda j: run_one(variant, out, *j), [j for j in jobs if j[0] not in done]))


if __name__ == "__main__":
    main(sys.argv[1:])
