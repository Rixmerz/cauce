"""A review's findings as one frozen ledger, and who decides each row.

Two blind reviewers do not vote: their findings are added together, so one
reviewer is enough to put a problem on the ledger and there is no tie to
break. What two reviews leave open is a finding only one of them saw and
could only read from the code. A third reader decides those, as a refuter,
never as a voter: it confirms or refutes each one with its own proof, or says
it cannot tell, and a row it cannot decide goes to the person, never to a
guess. A finding a command or the diff shows, or one both reviewers found,
needs no refuter.

The engine builds the ledger, never a worker: rows `F1..Fn` from the blocking
findings of the steps before (`findings.blocking`), in step order, with a
digest a brief carries. A fix is given only the rows it may fix and adds none;
a re-check answers only for those rows, against the fix's own diff. A row the
fix left open earns one more round, up to `ROUNDS`; after that, and for every
row the refuter could not decide, the run stops for the person as `review`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cauce import findings as findings_mod

ROLES = ("refute", "fix", "recheck")
#: Fix rounds before what is left goes to the person.
ROUNDS = 2
#: The answers each deciding role gives per row.
WORDS = {"refute": ("confirmed", "refuted", "unsure"), "recheck": ("fixed", "not_fixed", "regression")}
MAX_ROWS = 30


def _same(a: Mapping[str, str], b: Mapping[str, str]) -> bool:
    """The same finding from two reviewers: the same identity, or the same exact
    place. Two problems in one file are two rows; agreement is not a shared file."""
    if findings_mod.key(a) == findings_mod.key(b):
        return True
    where = a["where"].strip().lower()
    return bool(where) and where == b["where"].strip().lower()


def collect(sources: Sequence[tuple[str, Iterable[Mapping[str, str]]]]) -> list[dict[str, Any]]:
    """The ledger: each blocking finding once, with the steps that reported it."""
    rows: list[dict[str, Any]] = []
    for step, found in sources:
        for f in findings_mod.blocking(found):
            same = next((r for r in rows if _same(r, f)), None)
            if same is None:
                if len(rows) >= MAX_ROWS:
                    continue
                same = {k: f.get(k, "") for k in ("where", "what", "evidence_class", "cause", "proof")}
                same["seen_by"] = []
                rows.append(same)
            if step not in same["seen_by"]:
                same["seen_by"].append(step)
            if f["evidence_class"] == "deterministic":
                same["evidence_class"] = "deterministic"
    return [{"id": f"F{n}", **r} for n, r in enumerate(rows, 1)]


def disputed(row: Mapping[str, Any]) -> bool:
    """A row only one reviewer saw, read from the code and not shown: a refuter decides it."""
    return row["evidence_class"] != "deterministic" and len(row["seen_by"]) < 2


def digest(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps([{k: r[k] for k in sorted(r)} for r in rows], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def rows_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(f"- {r['id']} [{r['cause']}, {r['evidence_class']}, found by {', '.join(r['seen_by'])}] "
                     f"{r['where'] or 'unplaced'}: {r['what']}" + (f" — {r['proof']}" if r.get("proof") else "")
                     for r in rows)


def _refute(reviewers: int) -> str:
    """The refuter's ask, true to how the rows came: from one review or from two."""
    who = ("Two reviewers worked apart; each row below was found by only one of them and was read from the code, "
           "not shown." if reviewers > 1 else "A review read each row below from the code; nothing showed it.")
    return (f"You are the refuter. {who} Decide each row yourself, read-only, with your own proof: `confirmed` "
            "(you can show the problem, and, when a change is under review, that it caused or worsened it), "
            "`refuted` (you can show it is no problem, or that the change under review did not cause it) or "
            "`unsure` (you cannot tell; a person decides it). You are not voting: never decide a row by who "
            "reported it. Add no finding.")


_ASK = {
    "fix": ("Fix exactly the rows below, and nothing else: no other change, no new finding, no row rewritten. "
            "Run the project's tests before you stop. A row is fixed in the code it is about: writing it down "
            "(a note, a known-debt entry, a line in CLAUDE.md, AGENTS.md or another instruction file) fixes "
            "nothing, so never edit those files unless the row is about them. A row you cannot fix, or judge "
            "not worth fixing, leave as it is and say which and why in `summary`: the person decides it."),
    "recheck": ("Check, read-only, whether the fix resolved each row below: `fixed` (you can show it in the "
                "fix's diff, in the code the row is about), `not_fixed`, or `regression` (the fix broke "
                "something at or around it). A row the fix only wrote down (a note, a known-debt entry, a line "
                "in an instruction file) is `not_fixed`. Answer for these rows only and add no finding."),
}

#: Files a row is written down in rather than fixed: documentation and the instructions a repository keeps.
_NOTES = (".md", ".mdx", ".rst", ".adoc")
_INSTRUCTIONS = frozenset({".cursorrules", ".clinerules", ".windsurfrules"})


def written_down(path: str) -> bool:
    """A file a row can be written down in: documentation or a repository's instructions."""
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith(_NOTES) or name in _INSTRUCTIONS


def about_notes(row_where: str) -> bool:
    return written_down(row_where.split(":", 1)[0].strip())


def brief(role: str, rows: Sequence[Mapping[str, Any]], *, round_: int, sha: str, diff: str = "",
          reviewers: int = 2) -> str:
    ask = _refute(reviewers) if role == "refute" else _ASK[role]
    lines = [f"Ledger ({role}, round {round_} of {ROUNDS}), frozen; sha256 {sha}:", rows_text(rows), "", ask]
    if diff:
        lines.append(f"The fix is `{diff}`.")
    if role in WORDS:
        lines.append(f"Answer every row in `verdicts`, one entry per id, with `verdict` one of "
                     f"{', '.join(WORDS[role])} and the `proof` that shows it.")
    return "\n".join(lines)


def read(block: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """The verdicts a result block gives: id, verdict, proof; strings only."""
    raw = block.get("verdicts")
    if not isinstance(raw, list):
        return ()
    out = []
    for v in raw[:MAX_ROWS * 2]:
        if isinstance(v, dict) and isinstance(v.get("id"), str) and isinstance(v.get("verdict"), str):
            out.append({"id": v["id"].strip().upper(), "verdict": v["verdict"].strip().lower(),
                        "proof": str(v.get("proof") or "")[:500]})
    return tuple(out)


def missing(expected: Sequence[str], words: Sequence[str], verdicts: Sequence[Mapping[str, str]]) -> list[str]:
    """The rows a pass gives no valid verdict for."""
    said = {v["id"]: v["verdict"] for v in verdicts}
    return [i for i in expected if said.get(i) not in words]


@dataclass
class Outcome:
    """Where a deciding step leaves the ledger."""
    role: str
    round: int
    confirmed: list[str] = field(default_factory=list)
    refuted: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    survivors: list[str] = field(default_factory=list)
    #: Why the run stops for the person, or "" when it goes on.
    stop: str = ""

    def data(self) -> dict[str, Any]:
        return {"role": self.role, "round": self.round, "confirmed": self.confirmed, "refuted": self.refuted,
                "held": self.held, "survivors": self.survivors}


def outcome(spec: Mapping[str, Any], verdicts: Sequence[Mapping[str, str]]) -> Outcome:
    """What a refute or a re-check decided. `spec` is the step's ledger: role,
    round, its row ids, and for a refute the rows confirmed without one."""
    role, round_ = spec["role"], int(spec.get("round") or 1)
    said = {v["id"]: v["verdict"] for v in verdicts}
    ids, held = list(spec.get("ids") or ()), list(spec.get("held") or ())
    out = Outcome(role, round_)
    if role == "refute":
        out.confirmed = [*spec.get("agreed", ()), *(i for i in ids if said.get(i) == "confirmed")]
        out.refuted = [i for i in ids if said.get(i) == "refuted"]
        out.held = [i for i in ids if said.get(i) not in ("confirmed", "refuted")]
        if out.held and not out.confirmed:
            out.stop = f"the refuter could not decide {', '.join(out.held)}: read them and fix or dismiss"
        return out
    out.held = held
    # A fix whose diff only wrote things down fixed no row but the ones about those files,
    # whatever the re-check says: a confirmed row ends fixed in code, refuted, or with the person.
    noted = set(spec.get("noted_only") or ())
    out.survivors = [i for i in ids if said.get(i) != "fixed" or i in noted]
    if out.survivors and round_ >= ROUNDS:
        out.stop = f"{', '.join(out.survivors)} still open after {ROUNDS} fix rounds"
    elif not out.survivors and held:
        out.stop = f"the refuter could not decide {', '.join(held)}: read them and fix or dismiss"
    return out
