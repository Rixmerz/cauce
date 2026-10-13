"""How much a change can hurt, read from its request, its kind and what it
changed: `passive`, `medium` or `high`, with the reasons.

High is any of: it touches data (a migration, a schema, a delete), security
(auth, secrets, crypto, permissions), a contract others consume (an API spec,
a protocol, a public interface), concurrency, delivery (CI, deploy,
infrastructure), or it changes code with no test changed beside it, so nothing
would catch a mistake. A tier is only ever raised: the request sets where it
starts, and each attempt's files can raise it, never lower it. A change that
cannot be read (no checkout) is high: not knowing is not safe.

No model is asked. The tier decides one thing: whether a passing change is
checked again by an independent verifier before it counts.
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

TIERS = ("passive", "medium", "high")

_TEXT = (
    ("data", re.compile(r"\b(migrat\w*|schema|drop\s+(table|column)|backfill|delete\s+from|truncate|data ?loss|"
                        r"base de datos|database)\b", re.I)),
    ("security", re.compile(r"\b(auth\w*|login|password|secret|token|credential|crypt\w*|permission\w*|oauth|jwt|"
                            r"session cookie|csrf|xss|injection|vulnerab\w*|segurid\w*|contraseñ\w*)\b", re.I)),
    ("money", re.compile(r"\b(payment\w*|billing|invoice\w*|checkout|refund\w*|pago\w*|factur\w*|cobro\w*)\b", re.I)),
    ("concurrency", re.compile(r"\b(race|deadlock|concurren\w*|thread\w*|mutex|lock(ing)?|async|parallel\w*)\b",
                               re.I)),
    ("contract", re.compile(r"\b(public api|breaking change|endpoint\w*|protocol|webhook\w*|api contract|"
                            r"openapi|graphql schema)\b", re.I)),
    ("delivery", re.compile(r"\b(deploy\w*|ci pipeline|github actions|dockerfile|kubernetes|terraform|"
                            r"release|infra\w*)\b", re.I)),
)
_PATHS = (
    ("data", ("**/migrations/**", "**/migrate/**", "*.sql", "**/schema.*", "**/schema/**", "**/alembic/**",
              "**/prisma/schema.prisma")),
    ("security", ("**/auth/**", "**/*auth*.*", "**/*security*", "**/*crypto*", "**/*permission*", "**/.env*",
                  "**/*secret*")),
    ("contract", ("**/openapi*.*", "**/swagger*.*", "*.proto", "**/*.proto", "**/*.graphql", "**/api/**/*.d.ts")),
    ("delivery", (".github/workflows/*", "**/Dockerfile", "Dockerfile", "**/docker-compose*.yml", "*.tf",
                  "**/*.tf", "**/k8s/**", "**/helm/**", ".gitlab-ci.yml", "**/deploy/**")),
)
#: Kinds whose work touches nothing a person ships: no verifier.
_PASSIVE_KINDS = frozenset({"chat", "explore", "docs", "plan", "classify"})
_HIGH_KINDS = frozenset({"review-critical", "frontier"})
_DOCS = frozenset({".md", ".rst", ".txt", ".adoc"})
_TEST = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+$")
_CODE = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php",
                   ".swift", ".lua", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".sql", ".vue", ".svelte"})


@dataclass
class Risk:
    tier: str = "passive"
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, tier: str, reason: str) -> None:
        if TIERS.index(tier) > TIERS.index(self.tier):
            self.tier = tier
        if reason not in self.reasons:
            self.reasons.append(reason)

    def data(self) -> dict[str, object]:
        return {"tier": self.tier, "reasons": list(self.reasons)}


def of_request(text: str, kind: str) -> Risk:
    """Where a task's risk starts, from its words and its kind."""
    risk = Risk("passive" if kind in _PASSIVE_KINDS else "medium")
    if kind in _HIGH_KINDS:
        risk.raise_to("high", f"a {kind} task")
    for area, pattern in _TEXT:
        found = pattern.search(text)
        if found and kind not in _PASSIVE_KINDS:
            risk.raise_to("high", f"the request is about {area} ({found.group(0).strip()!r})")
    return risk


def _matches(path: str, patterns: Iterable[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


def of_change(risk: Risk, changed: Sequence[str] | None) -> Risk:
    """The tier raised by what an attempt changed. None means it could not be read."""
    if changed is None:
        risk.raise_to("high", "what changed could not be read")
        return risk
    for area, patterns in _PATHS:
        hit = next((p for p in changed if _matches(p, patterns)), None)
        if hit:
            risk.raise_to("high", f"it changed {area} ({hit})")
    code = [p for p in changed if PurePosixPath(p).suffix.lower() in _CODE and not _TEST.search(p)]
    tests = [p for p in changed if _TEST.search(p)]
    if code and not tests:
        risk.raise_to("high", f"it changed code ({code[0]}{' and more' if len(code) > 1 else ''}) "
                              "and no test beside it")
    elif code:
        risk.raise_to("medium", "it changed code and its tests")
    if changed and all(PurePosixPath(p).suffix.lower() in _DOCS for p in changed):
        risk.reasons.append("it changed documentation only")
    return risk


def needs_verifier(risk: Risk, *, cheap: bool) -> bool:
    """A change high in risk is always verified; a medium one when a cheap cell made it."""
    return risk.tier == "high" or (risk.tier == "medium" and cheap)
