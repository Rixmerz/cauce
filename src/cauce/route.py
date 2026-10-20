"""Which way a request is done: by the session itself, by one worker, or in parts side by side.

cauce started by sending work to workers. A worker costs a plan, a worktree, a
cold start on the code and, for a risky change, a verifier: for a small edit
the session that already holds the code does it faster, and a single strong
session is what most work wants. A worker earns its cost when the work is long
enough to run while the session goes on, or when the session's context is worth
protecting; parts earn theirs when the work splits into pieces that run at
once. `choose` reads the request with no model (its kind by rules, its size and
risk by the plan's own estimate, its listed items, the files and areas it names)
and the session's context, and says which, with its reasons. It is advice to the
orchestrating session, which decides; each decision is kept (`routed`) so the
choice can be measured against how the work went.

Standard library only, no model: the prompt hook runs it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from cauce import criteria as criteria_mod
from cauce import points as points_mod
from cauce import risk as risk_mod
from cauce.classify import by_rules, opens_with_change
from cauce.matrix import READ_ONLY_KINDS

MAIN, WORKER, PARALLEL = "main", "worker", "parallel"
#: At this size or under, the session does it: a worker's plan and cold start cost more than the work.
MAIN_POINTS = 2
#: At this size or over, or touching `PARALLEL_AREAS` areas or more, the work runs in parts.
PARALLEL_POINTS = 8
PARALLEL_AREAS = 2
#: Independent pieces a request lists, each naming its own top-level folder: parts.
PARALLEL_PIECES = 3
#: A session whose context is this full keeps it for what needs it: writing work goes to a worker.
CONTEXT_FULL = 0.6
#: The turns work of each size takes, at most the worker's measured median: a typo is not a feature.
TURNS_BY_POINTS = {1: 5, 2: 8, 3: 13, 5: 20, 8: 30, 13: 40}
#: A request this short is a turn of the conversation, never routed.
MIN_WORDS = 6
#: Words that point back into the conversation: the session holds what they mean, a worker does not.
_DEICTIC = re.compile(r"\b(esto|eso|aquello|lo (?:de|que) (?:arriba|antes|hablamos|dijimos)|"
                      r"como (?:dijimos|hablamos|antes)|this one|that one|as (?:we )?discussed|the above|"
                      r"as above|what we (?:said|discussed))\b", re.I)
#: A request that asks to understand rather than to change: its opening word, or a question.
_READS = re.compile(r"^\W*(explain|why|what|how|where|when|which|who|review|audit|investigate|analy[sz]e|compare|"
                    r"summari[sz]e|describe|explica|por qu[eé]|qu[eé]|c[oó]mo|d[oó]nde|cu[aá]l|revisa|audita|"
                    r"investiga|analiza|compara|resume|describe)\b", re.I)

#: A question about the work (whether, what if, is it worth it), asked mid-text as often as at its end:
#: a question mark after one of these openings is a turn of the conversation, never work to send.
_ASKS = re.compile(r"(?:^|[.!?¿]\s*)\W*(?:y |tambi[eé]n |pero |entonces )?(?:se )?(?:podr[ií]a\w*|"
                   r"deber[ií]a\w*|conviene|vale la pena|realmente|ser[ií]a|tendr[ií]a\w*|habr[ií]a|"
                   r"qu[eé] (?:pasa|tal|opinas|te parece)|y si|could|should|would|is it worth|what if|"
                   r"does it make sense|do we need|can we)\b[^?]*\?", re.I)

#: An opening that talks to the session itself: the answer is this conversation's to give.
_TALKS = re.compile(r"^\W*(?:habla\w*|h[aá]blame|dime|dinos|cu[eé]nta(?:me|nos)|"
                    r"res[uú]me(?:lo|me|nos)|resum[ií]\w*|responde\w*|contesta\w*|tell (?:me|us)|"
                    r"summari[sz]e|walk me through)\b", re.I)


def talks(text: str) -> bool:
    """Whether a request opens by talking to the session (tell me, habla, dime…):
    what it asks is an answer, and the session holding the conversation gives it."""
    return bool(_TALKS.search(text))


def asks(text: str) -> bool:
    """Whether a request is a question about the work rather than the work: an opening
    of doubt (could, should, what if, vale la pena…) ending in a question mark, at the
    start or after a sentence. A request that opens with a verb of change is an order,
    whatever it asks after it."""
    return bool(_ASKS.search(text)) and not opens_with_change(text)


@dataclass(frozen=True)
class Route:
    mode: str
    reasons: tuple[str, ...]
    kind: str = ""
    points: int = 0
    areas: tuple[str, ...] = field(default=())

    def advice(self) -> str:
        """What the orchestrating session is told; nothing when the session does it, the default."""
        why = "; ".join(self.reasons)
        if self.mode == WORKER:
            return (f"cauce route: a worker ({why}). Queue it (cauce's `queue` tool, or `cauce queue add`) with "
                    "the request written in full and its check, and go on here; do it yourself instead if it turns "
                    "out to need this conversation.")
        if self.mode == PARALLEL:
            return (f"cauce route: in parts, side by side ({why}). Run `cauce workflow run feature \"<the request>\"` "
                    "(it splits by itself), or queue one task per independent piece, each with its own `--surface`, "
                    "together; what depends on another piece goes `--after` it.")
        return ""

    def data(self) -> dict:
        return {"mode": self.mode, "reasons": list(self.reasons), "kind": self.kind, "points": self.points,
                "areas": list(self.areas)}


def _pieces(text: str) -> int:
    """How many of the request's listed items name a file in a top-level folder of their own."""
    tops = set()
    for item in criteria_mod.items(text):
        for path in points_mod.named(item):
            parts = PurePosixPath(path.strip("/")).parts
            if len(parts) > 1:
                tops.add(parts[0])
    return len(tops)


def _tokens(n: float) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def choose(text: str, *, context_tokens: int | None = None, context_share: float | None = None,
           footprint: tuple[int, int] | None = None) -> Route:
    """The way to do a request, with its reasons. Pure: everything it weighs is the request's
    words, the session's context (its tokens and how full it is) and what a worker attempt
    reads here as measured (`footprint`: median tokens and turns, `Store.worker_footprint`).

    Every turn a session takes re-reads its whole context, so work done here reads about
    the worker's turns times the session's context; a worker reads its own measured
    footprint, cold start included. The cheaper of the two is the way, once the request is
    work at all: reading, and what points back into the conversation, stay here; what splits
    runs in parts."""
    words = len(text.split())
    if words < MIN_WORDS:
        return Route(MAIN, ("a turn of the conversation",))
    if talks(text):
        return Route(MAIN, ("it talks to this session, which answers it",))
    if asks(text):
        return Route(MAIN, ("a question about the work, for this conversation to answer",))
    ruled = by_rules(text)
    # With no rule and no model, a question or an opening of reading is reading; else a build.
    kind = ruled.kind if ruled is not None else (
        "explore" if (_READS.search(text) or text.rstrip().endswith("?")) and not opens_with_change(text)
        else "implement")
    risk = risk_mod.of_request(text, kind)
    size = points_mod.of_request(text, kind, risk)
    areas = tuple(sorted({area for area, _ in risk_mod.areas(risk)}))
    pieces = _pieces(text)
    base = dict(kind=kind, points=size.points, areas=areas)
    if kind in READ_ONLY_KINDS:
        return Route(MAIN, (f"{kind}: reading, which the session holding the code does best",), **base)
    if size.points >= PARALLEL_POINTS or len(areas) >= PARALLEL_AREAS or pieces >= PARALLEL_PIECES:
        why = [f"{size.points} points"] if size.points >= PARALLEL_POINTS else []
        if len(areas) >= PARALLEL_AREAS:
            why.append(f"{len(areas)} areas ({', '.join(areas)})")
        if pieces >= PARALLEL_PIECES:
            why.append(f"{pieces} listed pieces in separate folders")
        return Route(PARALLEL, tuple(why), **base)
    if _DEICTIC.search(text):
        return Route(MAIN, ("it points back into this conversation, which a worker does not see",), **base)
    if footprint and context_tokens:
        measured_tokens, measured_turns = footprint
        # Each turn re-reads its context: a worker's measured reading per turn against this session's.
        turns = min(TURNS_BY_POINTS.get(size.points, measured_turns), measured_turns) or 1
        here, there = turns * context_tokens, turns * measured_tokens // max(measured_turns, 1)
        said = (f"done here it reads about {_tokens(here)} tokens ({turns} turns over this session's "
                f"{_tokens(context_tokens)} context), a worker about {_tokens(there)} at its measured "
                f"{_tokens(measured_tokens // max(measured_turns, 1))} a turn")
        if there < here:
            return Route(WORKER, (said, f"{size.points} points of {kind}"), **base)
        return Route(MAIN, (said,), **base)
    full = context_share is not None and context_share >= CONTEXT_FULL
    if size.points <= MAIN_POINTS and not full:
        return Route(MAIN, (f"{size.points} points: smaller than a worker's start-up",), **base)
    if full:
        return Route(WORKER, (f"this session's context is {context_share:.0%} full: keep it for what needs it",
                              f"{size.points} points of {kind}"), **base)
    return Route(WORKER, (f"{size.points} points of {kind}: long enough to run while the session goes on",), **base)
