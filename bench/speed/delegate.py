"""Which model should cauce's own small decisions run on: the classifier and the dispatcher?

Real calls, the same prompts and schemas cauce uses (`classify.by_model`, `dispatch.decide`), only
the model and effort changed, by rewriting the argv `classify.ask_haiku` builds:

- the classifier on `REQUESTS`, each labelled with the kinds that fit it (the rules are skipped:
  this measures the model's part), right when its kind is one of them;
- the dispatcher on `bench/speed/dispatch.py`'s pairs, right when it starts independent work and
  holds colliding work back.

    python bench/speed/delegate.py classify haiku/low,opus/low,sonnet/low 2
    python bench/speed/delegate.py dispatch haiku/low,opus/low 2
    python bench/speed/delegate.py report
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import dispatch as pairs  # noqa: E402  (bench/speed/dispatch.py)

from cauce import classify  # noqa: E402
from cauce import dispatch as cauce_dispatch  # noqa: E402

OUT = HERE / "results-delegate.jsonl"
#: A directory with no project instructions, as cauce gives its own classifier.
NEUTRAL = Path(tempfile.gettempdir()) / "cauce-delegate-neutral"

REQUESTS = [
    ("Agrega un endpoint /api/cursos que liste los cursos con paginación y tests", {"implement", "feature"}),
    ("Implementa el portal del cliente: login, lista de tickets, aprobar en QA y avisos por correo", {"feature"}),
    ("El botón de guardar en el formulario de proyecto quedó desalineado en el teléfono", {"ui"}),
    ("TypeError: Cannot read properties of undefined (reading 'id') at lib/server/tickets.ts:42 al abrir un ticket",
     {"debug-repro"}),
    ("A veces los tests de e2e fallan en CI pero nunca localmente, no sé por qué", {"debug-unclear"}),
    ("Revisa el módulo de pagos y el webhook de la pasarela buscando problemas de seguridad", {"review-critical"}),
    ("Revisa el PR de refactor del calendario y dime qué está mal", {"review-routine"}),
    ("¿Dónde se calcula la capacidad efectiva de cada persona?", {"explore", "chat"}),
    ("Explícame cómo funciona el reparto de utilidades", {"chat", "explore"}),
    ("Separa lib/server/crm.ts en módulos por responsabilidad sin cambiar comportamiento", {"refactor"}),
    ("Escribe tests para lib/mecha/calendar.ts cubriendo cambios de horario", {"test"}),
    ("Actualiza el README con cómo correr las pruebas e2e", {"docs"}),
    ("Diseña la migración de la base a multi-tenant con un plan por fases", {"plan"}),
    ("Rename apply_line to on_line across the codebase", {"refactor"}),
    ("Add a dark mode toggle to the settings page", {"ui", "implement"}),
    ("The export endpoint returns 500 when the cart is empty; the log says KeyError: 'items' in export.py line 31",
     {"debug-repro"}),
    ("Sometimes the dispatcher starts two workers for the same task, maybe a race", {"debug-unclear"}),
    ("Add retry with exponential backoff to the webhook sender", {"implement"}),
    ("Build a notifications system: in-app inbox, email digest, user preferences and a settings page", {"feature"}),
    ("Audit the auth middleware and session handling for vulnerabilities", {"review-critical"}),
    ("Write a CHANGELOG entry for 0.50.0", {"docs"}),
    ("Find every place where we read u.planIds directly", {"explore"}),
    ("Plan how to move from SQLite to Postgres without downtime", {"plan"}),
    ("Fix the flaky test test_flow::test_wake that fails 1 in 10 runs", {"test", "debug-unclear"}),
    ("Make the chart tooltips readable in dark mode", {"ui"}),
    ("Agrega validación de RUT al formulario de alta de clientes con mensajes de error", {"implement", "ui"}),
    ("Implement CSV export of invoices for finance", {"implement", "feature"}),
    ("Why does the queue sometimes stay paused after a resume?", {"debug-unclear", "chat"}),
    ("Review this diff for code quality issues", {"review-routine"}),
    ("Convert the callbacks in sync.ts to async/await", {"refactor"}),
    ("Agrega tests de seguridad para verifyRenderWebhook", {"test"}),
    ("Crea la página de alumno con su avance, su island y pruebas e2e", {"feature", "ui", "implement"}),
    ("Corrige el cálculo de HM: usa el plan vigente a la fecha en vez de planIds", {"implement", "debug-repro"}),
    ("Document the public functions in lib/secure.ts with JSDoc", {"docs"}),
    ("How many tickets are blocked per project? Just tell me", {"chat", "explore"}),
    ("Investigate why builds got 2x slower this week", {"debug-unclear", "explore"}),
]


def runner_for(cell: str):
    """`subprocess.run` with the model and effort `ask_haiku` asks for replaced by the cell's."""
    model, effort = cell.split("/")

    def run(argv, **kw):
        argv = list(argv)
        argv[argv.index("--model") + 1] = model
        argv[argv.index("--effort") + 1] = effort
        return subprocess.run(argv, **kw)
    return run


def one_classify(cell: str, n: int) -> dict:
    text, fits = REQUESTS[n]
    began = time.monotonic()
    found = classify.by_model(text, runner=runner_for(cell), cwd=NEUTRAL)
    return {"task": "classify", "cell": cell, "n": n, "kind": found.kind, "complexity": found.complexity,
            "source": found.source, "right": found.kind in fits, "s": round(time.monotonic() - began, 2),
            "cost": found.cost_usd}


def one_dispatch(cell: str, n: int) -> dict:
    label, ahead, changed, new = (pairs.PAIRS + pairs.HELD_OUT)[n]
    running = {"id": 101, "status": "running", "body": ahead, "options": "{}",
               **({"changed": changed} if changed is not None else {})}
    queued = {"id": 102, "status": "queued", "body": new, "options": "{}"}
    began = time.monotonic()
    got = cauce_dispatch.decide(queued, [running], runner=runner_for(cell))
    return {"task": "dispatch", "cell": cell, "n": n, "label": label, "parallel": got.parallel,
            "right": got.parallel == (label == "independent"), "s": round(time.monotonic() - began, 2),
            "cost": got.cost_usd}


def main(argv: list[str]) -> None:
    if argv[0] == "report":
        rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
        print(f"{'task':<10}{'cell':<14}{'n':>4}{'right':>8}{'s p50':>8}{'s p90':>8}{'$ total':>9}")
        for task, cell in sorted({(r["task"], r["cell"]) for r in rows}):
            rs = [r for r in rows if (r["task"], r["cell"]) == (task, cell)]
            s = sorted(r["s"] for r in rs)
            print(f"{task:<10}{cell:<14}{len(rs):>4}{sum(r['right'] for r in rs):>5}/{len(rs):<3}"
                  f"{median(s):>8.1f}{s[int(len(s) * 0.9)]:>8.1f}{sum(r['cost'] for r in rs):>9.3f}")
        for r in rows:
            if not r["right"]:
                what = r.get("kind") or ("started" if r.get("parallel") else "waited")
                print(f"  wrong: {r['task']} {r['cell']} #{r['n']}: {what}")
        return
    task, cells, reps = argv[0], argv[1].split(","), int(argv[2]) if len(argv) > 2 else 1
    count = len(REQUESTS) if task == "classify" else len(pairs.PAIRS) + len(pairs.HELD_OUT)
    one = one_classify if task == "classify" else one_dispatch
    jobs = [(c, n) for _ in range(reps) for c in cells for n in range(count)]
    with ThreadPoolExecutor(6) as pool:
        for row in pool.map(lambda j: one(*j), jobs):
            with OUT.open("a") as fh:
                fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
