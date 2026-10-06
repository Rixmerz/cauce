"""What a project knows, by topic: notes, the links between them, and the code they are about.

The memory of problems and fixes is global on purpose: a dead end in one
repository is a dead end in the next. What a project *is* is not: its business
rules, why its code is shaped the way it is, how it is run. That knowledge is
kept per project, in notes, so a session that compacted or a worker that just
started reads it back instead of rebuilding it from the code.

A library, not a heap:

- **Topics.** Every note is filed under one topic of a closed set (`TOPICS`,
  plus the ones a person adds to a project). Haiku files a note under a topic
  that exists; when none fits it may propose one, and a person decides.
- **Links.** Notes point at each other with a kind (`LINKS`): a business rule
  `depends_on` another, a decision `explains` a piece of code, a new fact
  `replaces` an old one. Recall follows them one step.
- **Anchors.** A note can be about a file or a symbol, as livespec ties a spec
  to code. An anchor remembers the file's content when the note was written; a
  later commit that changes it marks the note `review`. A note nobody checks
  again does not get to lie quietly.

Notes are written at three moments, never whenever a model feels like it: a
worker that passed lists what it `learned` in its result block; before a
session compacts or ends, Haiku reads what was said since the last time; and a
person keeps one with `cauce note`. They are read back by topic, by the code a
task touches and by full-text search inside the project, never another one.

Haiku files, and fails closed: when it cannot run, a fact is still kept, under
the topic its words point to (`filed_by: rule`), with no links.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from cauce import repo
from cauce.store import Store, home
from cauce.text import fold, reads_alike, title_of, words

#: The topics every project has. A person adds others per project.
TOPICS: dict[str, str] = {
    "business": "the domain: business rules, what users and the product need, the words the domain uses",
    "code": "how the code works: what a module or function does, its data flow, its traps",
    "decisions": "how the project evolved: why it is built this way, what was tried and dropped, migrations",
    "conventions": "how things are done here: naming, structure, style, review and commit habits",
    "environment": "how it is run, built, tested and deployed: commands, services, versions, configuration",
}
#: What a person may type for a topic, folded.
ALIASES = {
    "negocio": "business", "dominio": "business", "domain": "business",
    "codigo": "code", "logica": "code", "arquitectura": "code", "architecture": "code", "logic": "code",
    "decisiones": "decisions", "decision": "decisions", "evolucion": "decisions", "historia": "decisions",
    "history": "decisions", "evolution": "decisions",
    "convenciones": "conventions", "convencion": "conventions", "estilo": "conventions", "style": "conventions",
    "entorno": "environment", "env": "environment", "infra": "environment", "operacion": "environment",
    "ops": "environment",
}
LINKS = ("depends_on", "explains", "replaces", "contradicts", "example_of")
STATES = ("current", "review", "replaced", "dropped")
#: The states recall reads: a note to review is shown, marked as such.
LIVE = ("current", "review")
AUTHORS = ("person", "worker", "session")

#: Words that point a text at a topic: cheap routing for the hooks, and the
#: topic a fact gets when Haiku cannot file it. Matched on folded text.
_TOPIC_WORDS: dict[str, re.Pattern[str]] = {
    "business": re.compile(r"\b(negocio|cliente\w*|usuari[oa]s?|customers?|users?|business|regla de negocio|"
                           r"precio\w*|prices?|pricing|factura\w*|invoic\w*|pago\w*|payments?|suscripci\w*|subscription\w*|"
                           r"dominio|domain|producto|product)\b"),
    "decisions": re.compile(r"\b(por que|why|decidi\w*|decid\w*|decision\w*|evolu\w*|histori\w*|history|migra\w*|"
                            r"legacy|deprecat\w*|descart\w*|dropped|antes se|used to)\b"),
    "conventions": re.compile(r"\b(convenci\w*|conventions?|naming|nombra\w*|estilo|style|lint\w*|formato|format\w*|"
                              r"commit messages?|estructura de carpetas|folder structure)\b"),
    "environment": re.compile(r"\b(deploy\w*|despleg\w*|docker\w*|entorno|environment|build|compil\w*|ci|pipeline|"
                              r"servidor\w*|servers?|puerto|port|nvm|node version|version de node|instal\w*|install\w*|"
                              r"variables? de entorno|env vars?|\.env)\b"),
    "code": re.compile(r"\b(funci[oó]n\w*|functions?|m[oó]dul\w*|modules?|clase\w*|class\w*|endpoint\w*|component\w*|"
                       r"servicio\w*|service\w*|api|query|consulta\w*|flujo|flow|logica|logic)\b"),
}

#: A note shorter than this says nothing.
MIN_FACT = 12
MAX_FACT = 1000
MAX_FACTS = 8
#: Text in a fact that looks like a secret keeps the fact out: notes are put
#: in front of models and shown in the UI.
_SECRET = re.compile(r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_\w{8,}|xox[bap]-[\w-]{8,}|"
                     r"AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY|\b[0-9a-f]{40,}\b|password\s*[:=]\s*\S+)",
                     re.IGNORECASE)

Ask = Callable[..., tuple[dict | None, float, str]]


# --- topics -----------------------------------------------------------------


def topics(store: Store, project: str) -> dict[str, str]:
    """Every topic a project's notes may be filed under, with what it holds."""
    return {**TOPICS, **{t["name"]: t["description"] for t in store.note_topics(project)}}


def resolve_topic(store: Store, project: str, name: str) -> str:
    """The topic a person or a model named, or ValueError."""
    folded = fold(name).strip().replace(" ", "-")
    known = topics(store, project)
    found = folded if folded in known else ALIASES.get(folded)
    if found is None:
        raise ValueError(f"unknown topic {name!r}; known: {', '.join(known)} "
                         f"(add one with `cauce notes topic add <name> \"<what it holds>\"`)")
    return found


def topics_in(text: str) -> list[str]:
    """The topics a text's words point to, most specific first. Empty when none do."""
    folded = fold(text)
    return [t for t, pattern in _TOPIC_WORDS.items() if pattern.search(folded)]


def add_topic(store: Store, project: str, name: str, description: str) -> str:
    folded = fold(name).strip().replace(" ", "-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", folded):
        raise ValueError("a topic is one short word: letters, digits and dashes")
    if not description.strip():
        raise ValueError("say what the topic holds: it is what Haiku files by")
    store.add_note_topic(project, folded, description.strip())
    return folded


# --- writing ------------------------------------------------------------------


def clean_fact(text: str) -> str | None:
    """A fact as it is kept, or None when it is not worth keeping or must not be."""
    fact = " ".join(str(text or "").split())
    if len(fact) < MIN_FACT or _SECRET.search(fact):
        return None
    return fact[:MAX_FACT]


def add(
    store: Store,
    project: str,
    text: str,
    *,
    topic: str,
    author: str,
    filed_by: str,
    title: str | None = None,
    source: str = "",
    task_id: int | None = None,
    session_id: str | None = None,
    links: Iterable[tuple[int, str]] = (),
    paths: Iterable[str] = (),
    symbols: Iterable[tuple[str, str]] = (),
    repo_dir: Path | None = None,
    commit: str | None = None,
    proposed_topic: str | None = None,
) -> tuple[int, bool]:
    """Keep a note: (its id, whether it is new). A note that says what a live one
    in the same topic already says is that one, with the new anchors and links."""
    fact = clean_fact(text)
    if fact is None:
        raise ValueError("a note needs a fact of a few words, and never a secret")
    existing = same_as(store, project, topic, fact)
    if existing is None:
        note_id = store.add_note(project, topic, (title or "").strip()[:120] or title_of(fact, 80), fact,
                                 author=author, filed_by=filed_by, source=source, task_id=task_id,
                                 session_id=session_id, proposed_topic=proposed_topic)
    else:
        note_id = existing
    for to_id, kind in links:
        link(store, note_id, to_id, kind)
    if repo_dir is not None and (paths or symbols):
        anchor(store, note_id, repo_dir, paths, symbols, commit=commit)
    return note_id, existing is None


def same_as(store: Store, project: str, topic: str, fact: str) -> int | None:
    near = store.search_notes(project, fact, limit=10)
    for note in store.notes(project, topics=[topic], states=LIVE, ids=near):
        if reads_alike(fact, note["text"], 0.8):
            return int(note["id"])
    return None


def link(store: Store, from_id: int, to_id: int, kind: str) -> bool:
    """Link two notes of one project. `replaces` retires the note it replaces."""
    if kind not in LINKS:
        raise ValueError(f"unknown link {kind!r}; known: {', '.join(LINKS)}")
    a, b = store.get_note(from_id), store.get_note(to_id)
    if a is None or b is None or a["project"] != b["project"]:
        raise ValueError("both notes must exist, in the same project")
    made = store.link_notes(from_id, to_id, kind)
    if kind == "replaces" and b["state"] in LIVE:
        store.update_note(to_id, state="replaced", replaced_by=from_id, state_reason=f"replaced by #{from_id}")
    return made


# --- anchors and review -----------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30, check=False)


def _blobs(top: Path, commit: str, paths: Sequence[str]) -> dict[str, str]:
    """path -> blob at `commit`, for the paths that exist there."""
    if not paths:
        return {}
    out = _git(top, "ls-tree", "-r", commit, "--", *paths)
    found = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            found[path] = parts[2]
    return found


def anchor(store: Store, note_id: int, repo_dir: Path, paths: Iterable[str] = (),
           symbols: Iterable[tuple[str, str]] = (), *, commit: str | None = None) -> int:
    """Tie a note to files (relative to the checkout's top) and to livespec
    symbols, as (qualified name, path). Each anchor keeps the file's content
    at `commit` (default HEAD), which is what `review` compares against. Outside
    a checkout the anchor still names the file, for recall, and is never reviewed."""
    wanted = [(p.strip().lstrip("./"), None) for p in paths if p and p.strip()]
    wanted += [(path, symbol) for symbol, path in symbols if path]
    if not wanted:
        return 0
    top = repo.toplevel(repo_dir)
    sha = None
    blobs: dict[str, str] = {}
    if top is not None:
        sha = commit or _git(top, "rev-parse", "HEAD").stdout.strip() or None
        if sha:
            blobs = _blobs(top, sha, sorted({p for p, _ in wanted}))
    made = 0
    for path, symbol in dict.fromkeys(wanted):
        store.add_anchor(note_id, path, symbol=symbol, commit_sha=sha if path in blobs else None,
                         blob=blobs.get(path))
        made += 1
    return made


def review(store: Store, project: str, repo_dir: Path) -> list[tuple[int, str]]:
    """Mark `review` every current note whose anchored file changed since the note
    was written. A change only counts once it is in this checkout's history: an
    anchor on a branch not merged yet is left alone, and one whose content
    reached HEAD another way (a squash merge) moves to HEAD. Returns (note, why)."""
    top = repo.toplevel(repo_dir)
    if top is None:
        return []
    anchors = [a for a in store.project_anchors(project) if a["commit_sha"] and a["blob"]]
    if not anchors:
        return []
    head = _git(top, "rev-parse", "HEAD").stdout.strip()
    if not head:
        return []
    now = _blobs(top, head, sorted({a["path"] for a in anchors}))
    ancestors: dict[str, bool] = {}
    flagged: dict[int, str] = {}
    for a in anchors:
        current = now.get(a["path"])
        if current == a["blob"]:
            if a["commit_sha"] != head:
                store.update_anchor(a["id"], commit_sha=head, blob=current)
            continue
        sha = a["commit_sha"]
        if sha not in ancestors:
            ancestors[sha] = _git(top, "merge-base", "--is-ancestor", sha, head).returncode == 0
        if not ancestors[sha]:
            continue
        what = f"`{a['symbol']}` in {a['path']}" if a["symbol"] else a["path"]
        flagged.setdefault(a["note_id"], f"{what} {'changed' if current else 'was deleted'} "
                                         f"since this note was written ({sha[:7]} → {head[:7]})")
    for note_id, why in flagged.items():
        store.update_note(note_id, state="review", state_reason=why)
    return list(flagged.items())


def confirm(store: Store, note_id: int, repo_dir: Path | None) -> dict | None:
    """A person checked a note: it is current again, anchored to the code as it is now."""
    note = store.get_note(note_id)
    if note is None:
        return None
    if repo_dir is not None:
        top = repo.toplevel(repo_dir)
        head = _git(top, "rev-parse", "HEAD").stdout.strip() if top else ""
        anchors = store.note_anchors([note_id])
        blobs = _blobs(top, head, sorted({a["path"] for a in anchors})) if top and head else {}
        for a in anchors:
            store.update_anchor(a["id"], commit_sha=head if a["path"] in blobs else None, blob=blobs.get(a["path"]))
    return store.update_note(note_id, state="current", state_reason=None)


# --- reading ------------------------------------------------------------------


def recall(
    store: Store,
    project: str,
    query: str = "",
    *,
    in_topics: Sequence[str] | None = None,
    paths: Iterable[str] = (),
    limit: int = 8,
    hops: int = 1,
    states: Sequence[str] = LIVE,
    strict: bool = False,
) -> list[dict]:
    """The notes a question or a task needs, each with why it was picked: those
    anchored to the code it touches, those that match it in its topic, and the
    notes those link to, one step away. With neither a query nor paths, the
    latest of the topic. `strict` keeps only notes sharing several words with
    the query, for a hook that must not put loose matches in front of a model."""
    chosen: dict[int, str] = {}
    paths = list(dict.fromkeys(paths))
    anchored = store.notes_at(project, paths)
    for note in store.notes(project, states=states, ids=anchored):
        about = sorted({a["path"] for a in store.note_anchors([note["id"]]) if a["path"] in set(paths)})
        chosen.setdefault(int(note["id"]), "about " + ", ".join(about[:3]))
    if query.strip():
        ranked = store.search_notes(project, query, limit=50)
        rows = {n["id"]: n for n in store.notes(project, topics=in_topics, states=states, ids=ranked)}
        wanted = set(words(query))
        for note_id in ranked:
            note = rows.get(note_id)
            if note is None:
                continue
            if strict and len(wanted & set(words(f"{note['title']} {note['text']}"))) < min(3, len(wanted)):
                continue
            chosen.setdefault(note_id, "matches the question")
    elif not anchored:
        for note in store.notes(project, topics=in_topics, states=states, limit=limit):
            chosen.setdefault(int(note["id"]), "latest in its topic")
    picked = list(chosen)[:limit]
    if hops > 0 and picked:
        extra = max(2, limit // 2)
        for edge in store.note_links(picked):
            if extra <= 0:
                break
            other = edge["to_id"] if edge["from_id"] in picked else edge["from_id"]
            kind = edge["kind"]
            if other in chosen:
                continue
            note = store.get_note(other)
            if note is None or note["state"] not in states:
                continue
            seen_from = edge["from_id"] if edge["to_id"] == other else edge["to_id"]
            chosen[other] = f"linked to #{seen_from} ({kind})"
            picked.append(other)
            extra -= 1
    return expand(store, picked, chosen)


def expand(store: Store, ids: Sequence[int], why: Mapping[int, str] | None = None) -> list[dict]:
    """Notes with their anchors and links, in the order given."""
    if not ids:
        return []
    project_rows = {}
    for note_id in ids:
        note = store.get_note(note_id)
        if note is not None:
            project_rows[note_id] = note
    anchors: dict[int, list[dict]] = {}
    for a in store.note_anchors(list(project_rows)):
        anchors.setdefault(a["note_id"], []).append(
            {"path": a["path"], "symbol": a["symbol"], "commit": a["commit_sha"]})
    links: dict[int, list[dict]] = {}
    edges = store.note_links(list(project_rows))
    ends = {e[k] for e in edges for k in ("from_id", "to_id")} - set(project_rows)
    project = next(iter(project_rows.values()))["project"] if project_rows else ""
    dropped = {n["id"] for n in store.notes(project, states=["dropped"], ids=ends)} if ends else set()
    for edge in edges:
        if edge["from_id"] in dropped or edge["to_id"] in dropped:
            continue  # a note found wrong is no lead
        if edge["from_id"] in project_rows:
            links.setdefault(edge["from_id"], []).append({"to": edge["to_id"], "kind": edge["kind"], "out": True})
        if edge["to_id"] in project_rows:
            links.setdefault(edge["to_id"], []).append({"to": edge["from_id"], "kind": edge["kind"], "out": False})
    return [{**project_rows[i], "why": (why or {}).get(i, ""), "anchors": anchors.get(i, []),
             "links": links.get(i, [])} for i in ids if i in project_rows]


def line(note: Mapping[str, Any], *, text_chars: int = 400) -> str:
    """One note as a model reads it."""
    out = f"#{note['id']} [{note['topic']}] {note['title']}: {note['text'][:text_chars]}"
    if note["state"] == "review":
        out += f" (TO REVIEW: {note.get('state_reason') or 'its code changed'})"
    if note.get("anchors"):
        out += " · about " + ", ".join(
            (f"{a['symbol']} ({a['path']})" if a["symbol"] else a["path"]) for a in note["anchors"][:3])
    if note.get("links"):
        out += " · " + ", ".join(
            (f"{x['kind']} #{x['to']}" if x["out"] else f"#{x['to']} {x['kind']} this") for x in note["links"][:4])
    return out


def brief_lines(found: Sequence[Mapping[str, Any]]) -> list[str]:
    if not found:
        return []
    return ["What this project's notes say about it (cauce memory, kept from earlier work; check a note against "
            "the code before relying on it, and trust the code when they disagree):",
            *(f"- {line(n)}" for n in found)]


def index_text(store: Store, project: str, *, latest: int = 6) -> str | None:
    """The project's notes in a few lines: what topics hold, what is to review,
    the latest titles, and how to read the rest. What a session gets back after
    compacting, instead of the notes themselves."""
    counts = store.note_counts(project)
    live = {t: sum(n for s, n in by.items() if s in LIVE) for t, by in counts.items()}
    total = sum(live.values())
    if not total:
        return None
    parts = [f"{t} {n}" + (f" ({counts[t].get('review', 0)} to review)" if counts[t].get("review") else "")
             for t, n in sorted(live.items(), key=lambda kv: -kv[1]) if n]
    lines = [f"cauce notes: this project keeps {total} note(s) of what it knows — " + ", ".join(parts) + ".",
             "Read them before rebuilding context from the code: `cauce recall \"<question>\"` (add `--topic <t>` "
             "or `--path <file>`). Keep a durable fact the person tells you with `cauce note \"<fact>\" --topic <t>`, "
             f"where <t> is one of: {', '.join(topics(store, project))}."]
    recent = store.notes(project, states=LIVE, limit=latest)
    if recent:
        lines.append("Latest: " + "; ".join(f"#{n['id']} [{n['topic']}] {n['title']}" for n in recent))
    return "\n".join(lines)


# --- filing with Haiku ------------------------------------------------------

FILE_PROMPT = (
    "You file facts about one software project into its notes, a library kept by topic. You are given the "
    "project's topics, some of its existing notes, the files the work touched, and the new facts. For each fact "
    "answer: keep (false when it is trivial, only about this one task's progress, a guess, or plain from reading "
    "the code), topic (one of the listed topics, the one a person would look in), title (three to eight words, in"
    " the fact's own language, never translated), duplicate_of (the id of an existing note that already says it, "
    "else null), links (to existing notes it relates to, with a kind: depends_on, explains, replaces when the new"
    " fact makes that note wrong, contradicts, example_of), paths (only files from the given list the fact is "
    "about) and new_topic (a short name only when no topic fits; the fact is still filed under the closest one). "
    "Never invent ids."
)

EXTRACT_PROMPT = (
    "You read part of a conversation between a person and a coding agent working on one software project, and "
    "keep what is worth knowing the next time someone works on it, in the project's notes, a library kept by "
    "topic. Keep only durable facts the conversation established: business rules and domain words the person "
    "stated, decisions and why they were made, what was tried and dropped, conventions, how to run, build, test "
    "or deploy it, how a part of the code works. Never keep: the task's progress or plans, guesses, things later "
    "contradicted, anything about the agent itself, secrets, credentials, tokens or personal data. At most eight "
    "facts, often none. Each fact is one or two self-contained sentences, and it and its title are written in the "
    "language the person writes in: Spanish when they write Spanish, never translated. For each give topic (one "
    "of the listed), title (three to eight words), duplicate_of (the id of an existing note that already says "
    "it, else null), links to existing notes (depends_on, explains, replaces, contradicts, example_of), paths "
    "(only files the conversation itself names as what the fact is about; usually none) and new_topic (a short "
    "name only when no topic fits). Never invent ids."
)


def _entry_schema(names: Sequence[str], *, with_text: bool) -> dict[str, Any]:
    props: dict[str, Any] = {
        "topic": {"type": "string", "enum": list(names)},
        "title": {"type": "string"},
        "duplicate_of": {"type": ["integer", "null"]},
        "links": {"type": "array", "items": {
            "type": "object",
            "properties": {"to": {"type": "integer"}, "kind": {"type": "string", "enum": list(LINKS)}},
            "required": ["to", "kind"], "additionalProperties": False}},
        "paths": {"type": "array", "items": {"type": "string"}},
        "new_topic": {"type": ["string", "null"]},
    }
    if with_text:
        props = {"text": {"type": "string"}, **props}
    else:
        props = {"fact": {"type": "integer"}, "keep": {"type": "boolean"}, **props}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _list_of(key: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {key: {"type": "array", "items": item}}, "required": [key],
            "additionalProperties": False}


def file_schema(names: Sequence[str]) -> dict[str, Any]:
    return _list_of("notes", _entry_schema(names, with_text=False))


def extract_schema(names: Sequence[str]) -> dict[str, Any]:
    return _list_of("facts", _entry_schema(names, with_text=True))


def _candidates(store: Store, project: str, texts: Sequence[str], limit: int = 30) -> list[dict]:
    ids: list[int] = []
    for text in texts:
        ids += [i for i in store.search_notes(project, text, limit=5) if i not in ids]
    found = {n["id"]: n for n in store.notes(project, states=LIVE, ids=ids)}
    out = [found[i] for i in ids if i in found]
    for n in store.notes(project, states=LIVE, limit=10):
        if n["id"] not in found:
            out.append(n)
    return out[:limit]


def _question(store: Store, project: str, candidates: Sequence[Mapping], paths: Sequence[str], body: str) -> str:
    lines = ["Topics:", *(f"- {t}: {d}" for t, d in topics(store, project).items()), "", "Existing notes:"]
    lines += [f"#{n['id']} [{n['topic']}] {n['title']}: {n['text'][:200]}" for n in candidates] or ["(none yet)"]
    if paths:
        lines += ["", "Files the work touched:", *(f"- {p}" for p in paths[:40])]
    return "\n".join([*lines, "", body])


def _ask(ask: Ask | None) -> Ask:
    if ask is not None:
        return ask
    from cauce.classify import ask_haiku

    return ask_haiku


def file_facts(
    store: Store,
    project: str,
    facts: Sequence[str],
    *,
    author: str,
    source: str,
    task_id: int | None = None,
    session_id: str | None = None,
    repo_dir: Path | None = None,
    paths: Sequence[str] = (),
    commit: str | None = None,
    symbols_of: Callable[[str], list[tuple[str, str]]] | None = None,
    ask: Ask | None = None,
) -> list[dict]:
    """File what a worker learned: Haiku picks topic, title, links and the files
    each fact is about. Returns one entry per fact kept: {id, new, topic, title}."""
    kept = [f for f in (clean_fact(x) for x in facts[:MAX_FACTS]) if f]
    if not kept:
        return []
    names = list(topics(store, project))
    candidates = _candidates(store, project, kept)
    body = "Facts:\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(kept))
    out, _cost, _why = _ask(ask)(FILE_PROMPT, file_schema(names), _question(store, project, candidates, paths, body),
                                 cwd=home())
    decided: dict[int, dict] = {}
    for entry in (out or {}).get("notes") or []:
        if isinstance(entry, dict) and isinstance(entry.get("fact"), int) and 0 <= entry["fact"] < len(kept):
            decided.setdefault(entry["fact"], entry)
    known = {int(n["id"]) for n in candidates}
    filed = []
    for i, fact in enumerate(kept):
        entry = decided.get(i)
        if out is not None and entry is not None and not entry.get("keep", True):
            continue
        filed.append(_file_one(store, project, fact, entry if out is not None else None, names, known,
                               author=author, source=source, task_id=task_id, session_id=session_id,
                               repo_dir=repo_dir, allowed_paths=paths, commit=commit, symbols_of=symbols_of))
    return [f for f in filed if f]


def _file_one(store: Store, project: str, fact: str, entry: Mapping[str, Any] | None, names: Sequence[str],
              known: set[int], *, author: str, source: str, task_id: int | None, session_id: str | None,
              repo_dir: Path | None, allowed_paths: Sequence[str] | None, commit: str | None,
              symbols_of: Callable[[str], list[tuple[str, str]]] | None) -> dict | None:
    if entry is not None and entry.get("topic") in names:
        topic, filed_by = str(entry["topic"]), "haiku"
        title = str(entry.get("title") or "").strip() or None
        duplicate = entry.get("duplicate_of") if entry.get("duplicate_of") in known else None
        links = [(int(x["to"]), str(x["kind"])) for x in entry.get("links") or []
                 if isinstance(x, dict) and x.get("to") in known and x.get("kind") in LINKS]
        paths = [str(p) for p in entry.get("paths") or [] if isinstance(p, str)]
        proposed = str(entry.get("new_topic") or "").strip()[:40] or None
    else:
        found = topics_in(fact)
        topic, filed_by, title, duplicate, links, paths, proposed = (
            found[0] if found else "code", "rule", None, None, [], [], None)
    if allowed_paths is not None and allowed_paths:
        paths = [p for p in paths if p in set(allowed_paths)]
    elif repo_dir is not None:
        top = repo.toplevel(repo_dir) or repo_dir
        paths = [p for p in paths if (top / p).is_file()]
    else:
        paths = []
    symbols = symbols_of(fact) if symbols_of else []
    if duplicate is not None:
        for to_id, kind in links:
            link(store, duplicate, to_id, kind)
        if repo_dir is not None:
            anchor(store, duplicate, repo_dir, paths, symbols, commit=commit)
        note = store.get_note(duplicate)
        return {"id": duplicate, "new": False, "topic": note["topic"], "title": note["title"]}
    try:
        note_id, new = add(store, project, fact, topic=topic, author=author, filed_by=filed_by, title=title,
                           source=source, task_id=task_id, session_id=session_id, links=links, paths=paths,
                           symbols=symbols, repo_dir=repo_dir, commit=commit,
                           proposed_topic=proposed if proposed and proposed not in names else None)
    except ValueError:
        return None
    note = store.get_note(note_id)
    return {"id": note_id, "new": new, "topic": note["topic"], "title": note["title"]}


# --- what a session said --------------------------------------------------------

#: How much of a session's new conversation Haiku reads at once, the latest kept.
TRANSCRIPT_CHARS = 40_000
MESSAGE_CHARS = 3_000
#: Less new conversation than this is not worth a model call yet.
MIN_CONVERSATION = 80
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND = re.compile(r"^\s*<(command-[a-z-]+|local-command-[a-z-]+|bash-[a-z-]+|task-notification)>")


def conversation(path: Path, offset: int) -> tuple[str, int]:
    """The person's and the agent's words in a transcript after `offset`, without
    tool calls, tool output or injected context; and the offset reached."""
    from cauce.usage import _records

    said: list[str] = []
    end = offset
    try:
        for position, entry in _records(path, offset):
            end = position
            if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta") or (
                    entry.get("isCompactSummary")):
                continue  # a compaction's summary retells what was already read
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            else:
                texts = []
            for text in texts:
                text = _REMINDER.sub("", str(text)).strip()
                if not text or _COMMAND.match(text):
                    continue
                said.append(f"{'Person' if role == 'user' else 'Agent'}: {text[:MESSAGE_CHARS]}")
    except OSError:
        return "", offset
    return "\n\n".join(said)[-TRANSCRIPT_CHARS:], end


def extract(
    store: Store,
    project: str,
    session_id: str,
    transcript: Path,
    *,
    repo_dir: Path | None = None,
    symbols_of: Callable[[str], list[tuple[str, str]]] | None = None,
    ask: Ask | None = None,
) -> list[dict]:
    """Keep what a session established since the last extraction. The offset only
    moves when Haiku answered: a failed call is tried again next time."""
    key = f"notes:{transcript}"
    text, end = conversation(transcript, store.transcript_offset(key))
    if len(text) < MIN_CONVERSATION:
        # Too little to ask about yet: it stays unread, and is read with what
        # comes after it. One short sentence can be a rule worth keeping.
        return []
    names = list(topics(store, project))
    candidates = _candidates(store, project, [text[-4000:]])
    out, _cost, _why = _ask(ask)(EXTRACT_PROMPT, extract_schema(names),
                                 _question(store, project, candidates, (), "Conversation:\n" + text), cwd=home())
    if out is None:
        return []
    store.set_transcript_offset(key, end)
    known = {int(n["id"]) for n in candidates}
    filed = []
    for entry in (out.get("facts") or [])[:MAX_FACTS]:
        if not isinstance(entry, dict):
            continue
        fact = clean_fact(str(entry.get("text") or ""))
        if fact is None:
            continue
        # A file the conversation never names is a guess, and a wrong anchor
        # sends a right note to review.
        entry = {**entry, "paths": [p for p in entry.get("paths") or []
                                    if isinstance(p, str) and p and (p in text or Path(p).name in text)]}
        filed.append(_file_one(store, project, fact, entry, names, known, author="session",
                               source=f"session {session_id}", task_id=None, session_id=session_id,
                               repo_dir=repo_dir, allowed_paths=None, commit=None, symbols_of=symbols_of))
    return [f for f in filed if f]


Symbols = Callable[[str], list[tuple[str, str]]]


def livespec_symbols(repo_dir: Path, env: Mapping[str, str] | None = None) -> Symbols | None:
    """A lookup of the symbols a fact names, from the livespec index, read-only.
    None when livespec is off or the repository is not indexed."""
    from cauce import config

    if not config.enabled("livespec", env if env is not None else os.environ):
        return None
    from cauce.adapters.livespec import Livespec

    adapter = Livespec(env)
    status = adapter.inspect(repo_dir)
    if not status.present:
        return None
    return lambda fact: adapter.symbols_named(fact, status)


def to_json(found: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(found), ensure_ascii=False, default=str)
