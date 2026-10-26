"""Does the dispatcher's judgment hold independent work back?

Real: `dispatch.decide` (Haiku, its schema, no tools) on pairs of tasks in one repository: a
running one and a queued one. Each pair is labelled by what merging both would do:
`independent` (different features in different files: starting beside it saves the whole wait)
or `conflict` (one needs the other's change, or both change the same code: a wrong start costs a
merge conflict). The question is cauce's own (`dispatch.question`), with the files the running
task changed so far when the pair gives them.

    python bench/speed/dispatch.py current,named 3
    python bench/speed/dispatch.py report
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from cauce import dispatch  # noqa: E402

# (label, running task, its changed files so far or None, queued task)
PAIRS = [
    ("independent", "API: add an endpoint that exports invoices to CSV for finance, with tests",
     ["lib/server/finance.ts", "routes/api/invoices/export.ts"],
     "Student page: show the student's photo and formatted national id on their profile, with an e2e test"),
    ("independent", "Fix the flaky date test in tests/calendar_test.ts that fails around midnight", None,
     "Add a dark-mode toggle to the settings page"),
    ("independent", "Implementa el envío de correo de recuperación de contraseña con Resend",
     ["lib/server/email.ts", "routes/api/auth/forgot.ts"],
     "Agrega un gráfico de barras de horas por semana en el tablero del equipo"),
    ("independent", "Add retry with backoff to the GitHub webhook delivery handler", ["lib/server/github.ts"],
     "Translate the onboarding wizard copy to Spanish"),
    ("independent", "Refactor the chart kit's scale helpers in lib/charts/core/scale.ts", None,
     "Add pagination to the audit log API endpoint"),
    ("independent", "Build the CLI command `tool export --json` that dumps the store", ["src/tool/cli.py"],
     "Write the README section on installing on Windows"),
    ("independent", "Implementa el portal del cliente: página de avisos con su bandeja", None,
     "Corrige el redondeo de HH en el reporte de capacidad (lib/mecha/capacity.ts)"),
    ("independent", "Add a rate limiter to the login endpoint", ["routes/api/login.ts", "lib/server/limits.ts"],
     "Make the kanban board cards draggable between columns"),
    ("independent", "Upgrade the e2e harness to start the server on a random port", ["e2e/harness.ts"],
     "Add an index on tickets.project_id in a new migration"),
    ("independent", "API de cursos: endpoints para listar y crear cursos", ["routes/api/courses/index.ts"],
     "Página de login: muestra un error claro cuando la contraseña es incorrecta"),
    ("conflict", "API: build the course endpoints (list, create, edit) with validation",
     ["lib/server/courses.ts", "routes/api/courses/index.ts"],
     "Student page: list the courses the student is enrolled in, read from the new course endpoints"),
    ("conflict", "Rename the `User.planIds` field to `User.plans` everywhere", None,
     "Add a column to the users table showing each user's plans (User.planIds)"),
    ("conflict", "Rewrite the importer in src/importer.py to stream rows", ["src/importer.py"],
     "Add a --dry-run flag to the importer in src/importer.py"),
    ("conflict", "Migrate the database layer from raw SQL to the query builder across lib/db.ts and every caller",
     None, "Add a new collection `invoices` with its get/list/put in lib/db.ts"),
    ("conflict", "Implementa el modelo de datos de inscripciones y su tabla (migración 0042)", None,
     "Agrega la pantalla de inscripciones que guarda en la tabla de inscripciones nueva"),
    ("conflict", "Change the ticket status workflow: add a Blocked column between QA and Production",
     ["lib/server/tickets.ts"], "Show a count of tickets per status column on the project dashboard"),
    ("conflict", "Move every rule module into src/rules/line and src/rules/order and rename apply_line to on_line",
     ["src/rules/line/clearance.py"], "Add a new pricing rule: 5% off lines of 10 or more units"),
    ("conflict", "Fix the login form's validation in islands/LoginForm.tsx", ["islands/LoginForm.tsx"],
     "Add a 'remember me' checkbox to the login form"),
    ("conflict", "Bump the version to 0.47.0 and write its CHANGELOG entry", ["CHANGELOG.md", "pyproject.toml"],
     "Add the CHANGELOG entry for the new export flag"),
    ("conflict", "Replace the session store with signed cookies (lib/server/session.ts)", None,
     "Make sessions expire after 30 days of inactivity"),
    ("conflict", "Change money rounding from half-even to half-up in the pricing engine", ["src/tally/money.py"],
     "Add a 7% surcharge rule for express orders, with tests of the totals"),
    ("conflict", "Add server-side validation to the course create endpoint", ["routes/api/courses/index.ts"],
     "Add a description field to courses, editable from the course form"),
    ("conflict", "Implementa la pantalla de configuración de notificaciones del usuario", None,
     "Agrega el aviso por correo del resumen diario según la preferencia que el usuario configure"),
    ("conflict", "Split the 2,000-line orchestrate.py into modules plan.py, attempt.py and verify.py", None,
     "Make the verifier start beside the check in orchestrate.py"),
    ("conflict", "Switch the app's date handling to the Santiago calendar helper everywhere", None,
     "Fix the weekly report showing the wrong week on Sundays"),
    ("conflict", "Add role-based access checks to every /api route (guardRoute)", ["lib/server/access.ts"],
     "Add a new /api/reports endpoint for team leads"),
    ("independent", "Add tests for the CSV parser edge cases in tests/csv_test.ts", ["tests/csv_test.ts"],
     "Fix the typo in the onboarding email subject"),
    ("independent", "Implementa la API de pagos: webhook de la pasarela y registro del pago",
     ["routes/api/webhooks/payments.ts", "lib/server/payments.ts"],
     "Mejora la accesibilidad del menú lateral: foco visible y navegación con teclado"),
    ("independent", "Add a Gauge chart component to components/charts", ["components/charts/Gauge.tsx"],
     "Add an API endpoint that returns a project's open tickets as JSON"),
    ("independent", "Investigate and fix the memory leak in the background sync worker", ["lib/server/sync.ts"],
     "Add a FAQ page to the marketing site"),
]

# Written after the prompt was chosen, from real requests of this repository and a web app's: the held-out set.
HELD_OUT = [
    ("independent", "Habits: a reviewed habit installs itself once cauce's own check passes", ["src/cauce/habits.py"],
     "cauce ui: the task page shows a running worker's phase next to its status badge"),
    ("independent", "Fix the flaky test_flow dispatcher test that races the wake file", ["tests/test_flow.py"],
     "Add `cauce relay` docs to commands/relay.md"),
    ("independent", "Charts: add the Sankey component and its tests", ["lib/charts/core/sankey.ts"],
     "Portal: the client approves a ticket in QA from its detail page"),
    ("independent", "Implement the GitHub webhook signature check (X-Hub-Signature-256)", ["lib/server/github.ts"],
     "Seed data: add two demo projects with tickets in every column"),
    ("independent", "Loops: the runner stops a loop whose rounds did not improve in `stale` rounds", None,
     "Survey: read each repository's commit history for reverts"),
    ("independent", "Implementa la rotación de llaves con deno task rotate-key", ["scripts/rotate-key.ts"],
     "Ajusta el espaciado del encabezado de la página de proyectos en el teléfono"),
    ("conflict", "Add the `overlap` setting: the verifier starts beside cauce's check in orchestrate.py",
     ["src/cauce/orchestrate.py", "src/cauce/config.py"],
     "Add the `habit_auto` setting, on by default, copied from plugin options"),
    ("conflict", "Replace the dispatcher's lock file with an flock per lane in dispatch.py", ["src/cauce/dispatch.py"],
     "Make `cauce lanes --unpause` start the lane's dispatcher at once under its lock"),
    ("conflict", "Cotización: cambia el cálculo de HM para usar el plan vigente a la fecha (planIdsAt)",
     ["lib/mecha/quote.ts"], "Agrega al PDF de la cotización el desglose de HM por persona"),
    ("conflict", "Tickets: add the Blocked state and the event that releases it", ["lib/types.ts"],
     "Portal: the client releases a payment block on their ticket"),
    ("conflict", "Release 0.47.0: bump the four version files and write the CHANGELOG", ["CHANGELOG.md"],
     "Document the dispatcher's new re-judging in CHANGELOG and CLAUDE.md"),
    ("conflict", "Store: add a `seen` column to tasks and its migration", None,
     "Store: add an index on events(task_id, kind) in a new migration"),
]

NAMED = (
    "You decide whether a new software task can start now, beside other tasks in the same repository, "
    "or must wait until they finish. Each task runs in its own copy of the repository and its changes "
    "are merged afterwards. Waiting costs the whole time the work ahead takes; starting beside work it "
    "collides with costs a merge conflict. Answer parallel=false only for a collision you can name: the "
    "new task needs something another task is building (an endpoint, a table, a field, a function it "
    "will call), or both change the same feature, file, function, schema or behaviour (a rename, a "
    "migration or a refactor across the code collides with whatever it touches), as their texts, the "
    "surfaces they declared and the files a running task changed so far (read from git) show. Different "
    "features in different parts of the code are parallel=true even when neither declared its surfaces: "
    "a helper both might share, the same router, or the same test setup is a guess, not a collision. "
    "Reading-only tasks (questions, reviews, searches) can always run in parallel. What a task's text "
    "declares is a fact: a migration or version number it gives itself, while the other tasks take the "
    "other numbers, is no dependency on the tasks numbered before it. The reason is one short sentence: "
    "the collision and the task it is with, or why they do not meet."
)
NAMED2 = NAMED.replace(
    "(a rename, a migration or a refactor across the code collides with whatever it touches)",
    "(a rename, a migration or a refactor across the code collides with whatever it touches), or the new "
    "task's results rest on behaviour another task changes (a rounding, a format, a rule its totals or "
    "tests depend on)")
#: The prompt before 0.47, kept as the arm the others were measured against.
BEFORE = (
    "You decide whether a new software task can start now, at the same time as other tasks in "
    "the same repository, or must wait until they finish. Each task runs in its own copy of the "
    "repository and its changes are merged afterwards. Answer parallel=true only when the new "
    "task needs nothing another task will produce, and is unlikely to change the same files, the "
    "same functions or the same behaviour as any of them. Reading-only tasks (questions, reviews, "
    "searches) can always run in parallel. Each task is shown by the opening of its text and the "
    "lines further on (marked …) that say how it coordinates with other work. What a task's text "
    "declares is a fact: a migration or version number it gives itself, while the other tasks take "
    "the other numbers, is no dependency on the tasks numbered before it; only what its text says "
    "it needs from another task is. The surfaces a task declared are the only files it may change, "
    "and the files a running task changed so far are read from git: judge overlap on those, not on "
    "guesses. When unsure, answer false. The reason is one short sentence naming the task it "
    "conflicts with, if any."
)
PROMPTS = {"current": BEFORE, "named": NAMED, "named2": NAMED2}
assert NAMED2 == dispatch.SYSTEM_PROMPT, "cauce's prompt is the arm named2"
OUT = HERE / "results-dispatch.jsonl"


def one(arm: str, n: int) -> dict:
    label, ahead, changed, new = (PAIRS + HELD_OUT)[n]
    running = {"id": 101, "status": "running", "body": ahead, "options": "{}",
               **({"changed": changed} if changed is not None else {})}
    queued = {"id": 102, "status": "queued", "body": new, "options": "{}"}
    original = dispatch.SYSTEM_PROMPT
    out, cost, failure = dispatch.ask_haiku(PROMPTS[arm], dispatch.SCHEMA, dispatch.question(queued, [running]))
    assert original == dispatch.SYSTEM_PROMPT
    parallel = bool(out and out.get("parallel") is True)
    return {"arm": arm, "pair": n, "label": label, "parallel": parallel, "reason": (out or {}).get("reason") or failure,
            "cost": cost}


def main(argv: list[str]) -> None:
    if argv[0] == "report":
        rows = [json.loads(x) for x in OUT.read_text().splitlines() if x.strip()]
        if "--held-out" in argv:
            rows = [r for r in rows if r["pair"] >= len(PAIRS)]
        else:
            rows = [r for r in rows if r["pair"] < len(PAIRS)]
        print(f"{'arm':<9}{'independent started':>21}{'conflicts started':>19}{'n':>5}")
        for arm in sorted({r["arm"] for r in rows}):
            rs = [r for r in rows if r["arm"] == arm]
            ind = [r for r in rs if r["label"] == "independent"]
            con = [r for r in rs if r["label"] == "conflict"]
            print(f"{arm:<9}{sum(r['parallel'] for r in ind):>14}/{len(ind):<6}"
                  f"{sum(r['parallel'] for r in con):>12}/{len(con):<6}{len(rs):>5}")
        for r in rows:
            if (r["label"] == "conflict") == r["parallel"]:
                print(f"  wrong: {r['arm']} pair {r['pair']} ({r['label']}): {r['reason']}")
        return
    arms, reps = argv[0].split(","), int(argv[1]) if len(argv) > 1 else 2
    pairs = range(len(PAIRS), len(PAIRS) + len(HELD_OUT)) if "--held-out" in argv else range(len(PAIRS))
    jobs = [(a, n) for _ in range(reps) for a in arms for n in pairs]
    with ThreadPoolExecutor(6) as pool:
        for row in pool.map(lambda j: one(*j), jobs):
            with OUT.open("a") as fh:
                fh.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
