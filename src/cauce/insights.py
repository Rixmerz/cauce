"""What a person's own work says about the team they need: the facts the
personal-team skill builds teams from.

Read from what cauce already keeps and from the projects' checkouts, never
from a model: per project, the kinds of work asked for and how each went (pass
rate, where it passed, how it failed, what it cost), the sessions seen, the
languages its files are in, the commands its sessions repeat after an edit,
and which team it uses now. Beside them, the person's roles and teams and the
subagents they keep for Claude Code, by name and description only.

Nothing here writes. A project is read on its own: its tasks, its habits, its
files; nothing of one project's text is put beside another's. Notes are
counted by topic, never quoted.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cauce import habits, project, repo, roles, teams
from cauce.matrix import READ_ONLY_KINDS
from cauce.store import Store

#: A project's manifest → the language rules skill a specialization of it reads.
MANIFESTS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python"), ("requirements.txt", "python"), ("setup.py", "python"),
    ("tsconfig.json", "typescript"), ("package.json", "typescript"), ("go.mod", "go"), ("Cargo.toml", "rust"),
    ("pom.xml", "java"), ("build.gradle", "java"), ("build.gradle.kts", "kotlin"), ("Gemfile", "ruby"),
    ("composer.json", "php"), ("Package.swift", "swift"), ("CMakeLists.txt", "cpp"),
)
EXTENSIONS: Mapping[str, str] = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "typescript", ".jsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".lua": "lua", ".c": "cpp", ".cc": "cpp", ".cpp": "cpp",
    ".h": "cpp", ".hpp": "cpp", ".sh": "bash", ".sql": "sql",
}
#: Files that say a project has a user interface.
UI_EXTENSIONS = frozenset({".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html", ".astro"})
#: How many of a project's files are counted for its languages.
MAX_FILES = 20000


def languages(checkout: Path) -> dict[str, Any]:
    """A project's languages by share of its files, the manifests it holds, and
    whether it has a user interface."""
    files = roles.project_files(checkout)[:MAX_FILES]
    counts: Counter[str] = Counter()
    ui = 0
    for f in files:
        suffix = Path(f).suffix.lower()
        if suffix in EXTENSIONS:
            counts[EXTENSIONS[suffix]] += 1
        ui += suffix in UI_EXTENSIONS
    total = sum(counts.values())
    manifests = [name for name, _ in MANIFESTS if (checkout / name).exists()]
    if "tsconfig.json" in manifests and "package.json" in manifests:
        manifests.remove("package.json")
    return {
        "by_share": [{"language": lang, "files": n, "share": round(n / total, 2)} for lang, n in counts.most_common(6)]
        if total else [],
        "manifests": manifests,
        "ui_files": ui,
        "files": len(files),
    }


def work(store: Store, key: str) -> dict[str, Any]:
    """The tasks of one project by kind: how many, how each went, where it passed."""
    tasks = store.list_tasks(repo=key, source=("cauce", "queue"), limit=2000)
    by_kind: dict[str, dict[str, Any]] = {}
    for t in tasks:
        kind = t.get("kind") or "unknown"
        row = by_kind.setdefault(kind, {"kind": kind, "tasks": 0, "done": 0, "stopped": Counter(), "cost": 0.0,
                                        "passed_at": Counter()})
        row["tasks"] += 1
        row["cost"] += float(t.get("cost_usd") or 0)
        if t["status"] == "done":
            row["done"] += 1
            if t.get("final_cell"):
                row["passed_at"][t["final_cell"]] += 1
        elif t["status"] not in ("queued", "running"):
            row["stopped"][t["status"]] += 1
    rows = []
    for row in sorted(by_kind.values(), key=lambda r: -r["tasks"]):
        rows.append({"kind": row["kind"], "tasks": row["tasks"], "pass_rate": round(row["done"] / row["tasks"], 2),
                     "stopped": dict(row["stopped"]), "passed_at": dict(row["passed_at"].most_common(3)),
                     "avg_cost_usd": round(row["cost"] / row["tasks"], 2),
                     "reads": row["kind"] in READ_ONLY_KINDS})
    failures = Counter()
    for t in tasks:
        for a in store.attempts(t["id"]) if t["status"] != "done" else ():
            if a.get("failure"):
                failures[a["failure"]] += 1
    return {"tasks": len(tasks), "by_kind": rows, "failures": dict(failures.most_common(5)),
            "median_cost_usd": round(statistics.median([float(t.get("cost_usd") or 0) for t in tasks]), 2)
            if tasks else 0.0}


def checks(store: Store, key: str) -> list[str]:
    """The commands this project's sessions run after an edit, best first."""
    habits.load_events(store)
    here, _ = habits.for_repo(store.tool_events(days=90), key)
    return [" → ".join(c.steps) for c in here[:5]]


def claude_agents(env: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """The subagents the person keeps for Claude Code, by name and description:
    a hint of how they like work split, never instructions for a worker."""
    out = []
    folder = roles.claude_dir(env) / "agents"
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            meta, _ = roles.split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, roles.RoleError):
            continue
        name, description = meta.get("name"), meta.get("description")
        if isinstance(name, str) and isinstance(description, str):
            out.append({"name": name, "description": description[:300]})
    return out


def purpose(found: Mapping[str, Any]) -> list[str]:
    """What a project's work is mostly about, in a few words, from its kinds and files."""
    words = []
    kinds = {r["kind"]: r["tasks"] for r in found["work"]["by_kind"]}
    total = sum(kinds.values()) or 1
    if found["languages"]["ui_files"] or kinds.get("ui", 0) / total >= 0.2:
        words.append("user interface")
    if sum(kinds.get(k, 0) for k in ("debug-repro", "debug-unclear")) / total >= 0.25:
        words.append("debugging")
    if sum(kinds.get(k, 0) for k in ("review-routine", "review-critical")) / total >= 0.2:
        words.append("review")
    if kinds.get("docs", 0) / total >= 0.25:
        words.append("documentation")
    if sum(kinds.get(k, 0) for k in ("implement", "feature", "refactor")) / total >= 0.3:
        words.append("building")
    langs = [x["language"] for x in found["languages"]["by_share"] if x["share"] >= 0.2]
    return [*words, *langs] or ["general"]


def suggestions(found: Mapping[str, Any]) -> list[str]:
    """What the facts point at, for the person to accept or not. Never applied."""
    out = []
    for lang in [x["language"] for x in found["languages"]["by_share"] if x["share"] >= 0.2]:
        skill = f"{lang}-rules"
        if roles.find_skill(skill) is not None:
            manifest = next((m for m, lg in MANIFESTS if lg == lang and m in found["languages"]["manifests"]), None)
            match = manifest or f"**/*{next(e for e, lg in EXTENSIONS.items() if lg == lang)}"
            out.append(f"a {lang} implementer: `cauce roles new implementer-{lang} --extends implementer "
                       f"--match {match} --skill {skill}`")
    for row in found["work"]["by_kind"]:
        if row["tasks"] >= 3 and row["pass_rate"] < 0.5:
            out.append(f"{row['kind']} passes {int(row['pass_rate'] * 100)}% here: give its role this project's "
                       "conventions and checks in its instructions")
    unused = [k for k in roles.ROLE_KINDS if k not in {r["kind"] for r in found["work"]["by_kind"]}]
    if found["work"]["tasks"] >= 10 and unused:
        out.append(f"never asked for here: {', '.join(unused)}; a team for this project can leave them out")
    if found["checks"]:
        out.append(f"the sessions here check with `{found['checks'][0]}`: name it in the roles that write")
    return out


def gather(store: Store, *, limit: int = 20, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every project cauce has worked in, with what its work says, and the person's
    roles, teams and Claude Code subagents."""
    catalog = roles.load()
    found_teams, _ = teams.listing(catalog)
    projects = []
    for p in store.projects()[:limit]:
        where = Path(p["cwd"]) if p.get("cwd") else None
        top = repo.toplevel(where) if where is not None and where.is_dir() else None
        checkout = top or (where if where is not None and where.is_dir() else None)
        entry: dict[str, Any] = {
            "repo": p["repo"], "dir": str(checkout or p.get("cwd") or ""), "exists": checkout is not None,
            "enrolled": project.find(where) is not None if where is not None and where.is_dir() else False,
            "sessions": p.get("sessions", 0), "last_seen": p.get("last_seen"),
            "team": teams.of(p["repo"], catalog)[0].name,
            "languages": languages(checkout) if checkout else {"by_share": [], "manifests": [], "ui_files": 0,
                                                                "files": 0},
            "work": work(store, p["repo"]), "checks": checks(store, p["repo"]),
        }
        entry["purpose"] = purpose(entry)
        entry["suggestions"] = suggestions(entry)
        projects.append(entry)
    return {
        "projects": projects,
        "roles": [{"name": r.name, "scope": r.scope, "kinds": list(r.kinds), "description": r.description,
                   "extends": r.extends, "derived_from": r.derived_from} for r in catalog.roles.values()],
        "teams": [{"name": t.name, "scope": t.scope, "roles": list(t.role_names), "description": t.description,
                   "projects": teams.projects_of(t.name)} for t in found_teams],
        "claude_agents": claude_agents(env),
        "skills": [s.name for s in roles.bundled_skills()],
    }


def text(found: Mapping[str, Any]) -> str:
    """The same, for a person reading a terminal."""
    lines = []
    for p in found["projects"]:
        lines.append(f"{p['repo']} ({p['dir'] or 'no checkout here'}): team {p['team']}, "
                     f"{p['work']['tasks']} tasks, {p['sessions']} sessions; about {', '.join(p['purpose'])}")
        langs = ", ".join(f"{x['language']} {int(x['share'] * 100)}%" for x in p["languages"]["by_share"])
        if langs:
            lines.append(f"  languages: {langs}")
        for row in p["work"]["by_kind"][:6]:
            lines.append(f"  {row['kind']:<16} {row['tasks']:>3} tasks, {int(row['pass_rate'] * 100):>3}% passed, "
                         f"${row['avg_cost_usd']:.2f} each")
        for s in p["suggestions"]:
            lines.append(f"  → {s}")
    if not found["projects"]:
        lines.append("cauce has not worked in any project yet: run some work through it first")
    lines.append(f"roles: {len(found['roles'])}; teams: {', '.join(t['name'] for t in found['teams'])}")
    if found["claude_agents"]:
        lines.append("your Claude Code subagents: " + ", ".join(a["name"] for a in found["claude_agents"]))
    return "\n".join(lines)


def as_json(found: Mapping[str, Any]) -> str:
    return json.dumps(found, indent=2, ensure_ascii=False, default=str)
