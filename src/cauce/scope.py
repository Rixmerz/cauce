"""Words that leave a request's scope open: "at least X", "check for others".

A request that names what to change is closed: the worker changes that. One
that says "neutralise at least ENVIRONMENT; check env.js for others" hands the
worker the call on how far to go, and a worker given that call tends to go
further than the evidence needs: it removes three things where one was wanted,
and the extra round is spent undoing it. The words are the person's, so cauce
never rewrites them: it says which ones are open before the run (a person can
cancel and close them) and tells the worker to hold every change made under
them to the evidence.

No model is asked. The Spanish matches what people type and stays.
"""
from __future__ import annotations

import re

_OPEN = re.compile(
    r"\b(?:"
    # English
    r"at (?:the )?(?:very )?least|at a minimum"
    r"|(?:check|look|search|scan)\b(?:[^.;:\n]|[.:](?=\S)){0,40}?"
    r"\b(?:for|if there are) (?:any )?(?:others?|similar ones)\b"
    r"|(?:and|or) any (?:others?|similar|related)\b|and so on|and the like|or similar\b"
    r"|whatever (?:else )?(?:is |seems )?(?:needed|necessary|required)|as (?:needed|necessary)\b"
    r"|wherever (?:needed|necessary|it applies)|anything else\b"
    # Spanish
    r"|al menos|por lo menos|como m[ií]nimo"
    r"|(?:revisa|revisar|busca|buscar|mira|chequea)\b(?:[^.;:\n]|[.:](?=\S)){0,40}?\b(?:otr[oa]s|similares)\b"
    r"|(?:y|o) cualquier otr[oa]|y dem[aá]s\b|y similares|o similares|o parecid[oa]s"
    r"|lo que (?:haga|sea) (?:falta|necesario)|donde (?:haga falta|sea necesario)|todo lo que encuentres"
    r")",
    re.IGNORECASE)
_ETC = re.compile(r"\betc\b\.?", re.IGNORECASE)


def open_phrases(text: str) -> list[str]:
    """The phrases that leave the request's scope open, verbatim and in order, once each."""
    found = [m.group(0).strip() for m in _OPEN.finditer(text)]
    found += [m.group(0).strip() for m in _ETC.finditer(text)]
    return list(dict.fromkeys(found))


def warning(phrases: list[str]) -> str:
    """The plan's note on an open request, or "" for a closed one."""
    if not phrases:
        return ""
    quoted = ", ".join(f"«{p}»" for p in phrases[:4])
    return (f"the request leaves its scope open ({quoted}): a worker tends to change more than needed there. "
            "Naming each thing to change closes it")


def brief(phrases: list[str]) -> str:
    """What a worker is told about the open words, or "" for a closed request."""
    if not phrases:
        return ""
    quoted = ", ".join(f"«{p}»" for p in phrases[:4])
    return (f"The request leaves its scope open ({quoted}). Change what it names. Change anything more under "
            "those words only when you can show it is required (a check that fails without it, a reference that "
            "breaks); everything else you considered, leave as it is and list it in `summary` as considered and "
            "left unchanged.")
