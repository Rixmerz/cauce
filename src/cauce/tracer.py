"""The TRACER method in a repository that keeps it (`docs/tracer/`): read, never written.

TRACER (github.com/Rixmerz/tracer) defines a product from its audience's side before it is built:
audience profiles, critical flows whose acceptance lines come in two halves (WORKS, the technical
result; CORRECT, what the person sees and can do), component floors and a catalogue of UI/UX
anti-patterns. Its phases that need the person (the contract, the empathy round, the scope filter)
stay in the session; what cauce does is carry the written method into the work it runs:

- a writing worker gets the audience, the floors, the anti-patterns, the identity and the flows its
  request names, as data, in its brief;
- the independent verifier gets the CORRECT lines of those flows and the anti-pattern catalogue as
  what the change must meet: a line that does not hold, or an anti-pattern shown, is a finding.

Nothing here is a model call, and nothing is written in the repository.
"""
from __future__ import annotations

from pathlib import Path

from cauce.text import clip, fold, words

FOLDER = Path("docs") / "tracer"
#: The method's files a worker reads before any code, in this order, and what each holds.
FILES = (("audiences.md", "Audience profiles"), ("identity.md", "Identity guideline"),
         ("floors.md", "Component floors"), ("anti-patterns.md", "Anti-pattern catalogue"))
#: The most of the method one brief carries; what is left out is said by count, with where it is.
BRIEF_CHARS = 8000


def present(repo_dir: Path) -> bool:
    return (repo_dir / FOLDER).is_dir()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def flows_for(repo_dir: Path, text: str) -> list[Path]:
    """The critical flows a request reaches: a file under `docs/tracer/flows/` whose name's words all
    appear in the request, or begin one of its words (`flows/print-receipt.md` for "...the printed
    receipts..."). Strict on purpose:
    a flow briefed by a loose match puts another capability's CORRECT lines on the work."""
    folder = repo_dir / FOLDER / "flows"
    if not folder.is_dir():
        return []
    asked = set(words(fold(text)))
    out = []
    for path in sorted(folder.glob("*.md")):
        named = [w for w in words(fold(path.stem.replace("-", " ").replace("_", " "))) if len(w) > 2]
        if named and all(any(a == w or (len(w) >= 4 and a.startswith(w)) for a in asked) for w in named):
            out.append(path)
    return out


def _sections(repo_dir: Path, text: str) -> list[tuple[str, str]]:
    found = [(f"{title} ({(FOLDER / name).as_posix()})", _read(repo_dir / FOLDER / name)) for name, title in FILES]
    found += [(f"Critical flow ({path.relative_to(repo_dir).as_posix()})", _read(path))
              for path in flows_for(repo_dir, text)]
    return [(title, body) for title, body in found if body]


def brief_lines(repo_dir: Path, text: str) -> list[str]:
    """What a writing worker reads before any code: the method's files, as data."""
    if not present(repo_dir):
        return []
    sections = _sections(repo_dir, text)
    lines = [f"This repository follows the TRACER method (`{FOLDER.as_posix()}/`): done means correct for the "
             "people who use it, checked through the interface they use, never only that tests pass. Read these "
             "before any code. Never fill a detail they leave open with the most common solution: say it in your "
             "result instead. Each CORRECT line of a flow below is part of what your work must meet."]
    room, left = BRIEF_CHARS, 0
    for title, body in sections:
        if room <= 200:
            left += 1
            continue
        shown = clip(body, room)
        room -= len(shown)
        lines.append(f"{title}:\n{shown}")
    if left:
        lines.append(f"{left} more of the method's files did not fit here: read them in `{FOLDER.as_posix()}/`.")
    if not flows_for(repo_dir, text) and (repo_dir / FOLDER / "flows").is_dir():
        lines.append(f"No critical flow under `{FOLDER.as_posix()}/flows/` is named by this request: read the "
                     "ones your change reaches.")
    return lines


#: How the measuring neighbours check what TRACER asks of an interface, when the check has them.
MEASURED_BY = {
    "layout-inspector": "Measure the floors and anti-patterns on the rendered page instead of judging them by eye: "
                        "`detect_issues` (overflow, clipping, overlap) and `compare_viewports` for every breakpoint "
                        "the audience uses, `accessibility_spatial` for keyboard order, focus, target sizes and "
                        "contrast, `element_context` for the cause of what fails. A floor or anti-pattern it shows "
                        "broken is a `deterministic` finding with the measurement as its proof.",
}


def verifier_lines(repo_dir: Path, text: str, measures: tuple[str, ...] = ()) -> list[str]:
    """What the independent check must hold the change to: the CORRECT lines of the flows the
    request names and the anti-pattern catalogue, judged as the audience would; with a measuring
    neighbour it has (`measures`), how to measure them."""
    if not present(repo_dir):
        return []
    correct = []
    for path in flows_for(repo_dir, text):
        correct += [f"- {path.stem}: {line.strip().lstrip('-|').strip()}" for line in _read(path).splitlines()
                    if "CORRECT" in line and line.strip().lstrip("-|").strip() not in ("CORRECT", "")]
    catalogue = _read(repo_dir / FOLDER / "anti-patterns.md")
    if not correct and not catalogue:
        return []
    out = [f"This repository follows the TRACER method: judge the change as the audience in "
           f"`{FOLDER.as_posix()}/audiences.md` would. A CORRECT line below that does not hold, or an anti-pattern "
           "of the catalogue the change shows, is a finding, shown with the step or the rendered text that proves it."]
    if correct:
        out.append("CORRECT lines of the flows this request names:\n" + clip("\n".join(correct), BRIEF_CHARS // 2))
    if catalogue:
        out.append("Anti-pattern catalogue:\n" + clip(catalogue, BRIEF_CHARS // 2))
    out += [MEASURED_BY[name] for name in measures if name in MEASURED_BY]
    return out
