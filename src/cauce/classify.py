"""What kind of work a request is.

Two passes. **Rules** first: a handful of patterns that are right when they
fire — an explicit `#kind` tag, a stack trace, a bare "commit and push". They
cost nothing and they are deterministic, so they decide everything they can.
They are deliberately *not* asked to decide everything: a keyword list that
must classify every request ends up ordering its rules so the first match wins,
and "the header no longer looks right" lands in debug because of "no longer".

Everything the rules do not claim goes to **Haiku**, with a JSON schema and no
tools. A request classified by a model below the confidence floor, or a model
that could not be run, is the default kind — which is the middle of the matrix,
not a guess.

`frontier` (Fable) is never a model's decision. Only the `#fable` tag reaches it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cauce.matrix import DEFAULT_KIND, KINDS
from cauce.text import fold, words

COMPLEXITIES = ("trivial", "low", "medium", "high")
MIN_CONFIDENCE = 0.6
CLASSIFIER_TIMEOUT_S = 60

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Classification:
    kind: str
    complexity: str = "medium"
    source: str = "default"  # rule | model | default
    reason: str = ""
    cost_usd: float = 0.0


# --- rules ----------------------------------------------------------------

_CRITICAL = re.compile(
    r"\b(auth\w*|login|contrasena\w*|password\w*|token\w*|pagos?|payments?|cripto\w*|crypto\w*"
    r"|seguridad|security|permis\w*|secret\w*|credencial\w*|credentials?)\b"
)

#: (pattern, kind, reason). Order matters only where two could fire; each one is
#: written to be right on its own.
_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(traceback|stack ?trace|^\s*\w*error:|\berror: |exit code \d|codigo de salida \d"
                   r"|\bline \d+|\blinea \d+)", re.M),
        "debug-repro",
        "carries an error, a trace or a line number",
    ),
    (
        re.compile(r"\b(intermitente\w*|flaky|race condition|aleatoriamente|a veces falla|sometimes fails"
                   r"|intermittent\w*|sporadic\w*|no se (por que|la causa)|no reproduce\w*)\b"),
        "debug-unclear",
        "a failure without a clear cause",
    ),
    (
        re.compile(r"\b(code review|haz (un )?review|review (the|this|my) (pr|diff|change)"
                   r"|revisa (el|la|los|este|esta|mi) (pr|diff|codigo|cambios?|pull request))\b"),
        "review",
        "asks for a review",
    ),
    (
        re.compile(r"\b(arquitectura|architecture|plan tecnico|technical plan|disena (el|un) sistema"
                   r"|design (the|a) system|roadmap|propuesta tecnica|trade-?offs?)\b"),
        "plan",
        "asks for a plan or an architecture",
    ),
    (
        re.compile(r"\b(donde (esta|se define|se usa)|where (is|are|do we)|find where|busca (donde|el|la|los)"
                   r"|encuentra (donde|el|la)|lista (los|las) archivos)\b"),
        "explore",
        "asks where something is",
    ),
)

_DOCS_ONLY = re.compile(r"\b(commit\w*|push|changelog|readme|release notes?|docstrings?)\b")
_QUESTION = re.compile(
    r"^\s*[¿¡]?\s*(que|como|por que|cual|cuando|what|how|why|which|when|explica\w*|explain|is it|es)\b"
)
_TEST = re.compile(r"\b(escribe|agrega|anade|add|write|corre|run|arregla|fix)\s+(\w+\s+){0,2}(tests?|pruebas?)\b")
_TAG = re.compile(r"#([a-z][a-z-]+)\b")


def by_rules(text: str) -> Classification | None:
    folded = fold(text)
    for tag in _TAG.findall(folded):
        if tag == "fable":
            return Classification("frontier", "high", "rule", "tagged #fable")
        if tag in KINDS:
            return Classification(tag, "medium", "rule", f"tagged #{tag}")
    for pattern, kind, reason in _RULES:
        if pattern.search(folded):
            if kind == "review":
                critical = _CRITICAL.search(folded)
                kind = "review-critical" if critical else "review-routine"
            return Classification(kind, "medium", "rule", reason)
    tokens = words(text)
    if _DOCS_ONLY.search(folded) and len(tokens) <= 12:
        return Classification("docs", "low", "rule", "a short commit, push or docs request")
    if _TEST.search(folded):
        return Classification("test", "medium", "rule", "asks for tests")
    if _QUESTION.search(folded) and folded.rstrip().endswith("?") and len(tokens) <= 25:
        return Classification("chat", "low", "rule", "a short question")
    return None


# --- the model -------------------------------------------------------------

_KIND_HELP: Mapping[str, str] = {
    "chat": "a question, an explanation or a summary; nothing to change",
    "explore": "find files, code or facts in a repository; nothing to change",
    "docs": "README, changelog, docstrings, a commit message",
    "test": "write, fix or run tests",
    "refactor": "restructure code without new behaviour",
    "implement": "carry out a change whose shape is already clear",
    "ui": "user interface, layout, styling",
    "feature": "a new capability that crosses several parts of the code",
    "debug-repro": "a failure with a clear error or reproducible steps",
    "debug-unclear": "a failure whose cause is unknown, intermittent or hard to reproduce",
    "review-routine": "review ordinary code",
    "review-critical": "review code touching auth, money, crypto or security",
    "plan": "design an architecture, a migration or a technical plan",
}

SYSTEM_PROMPT = (
    "You classify a software task by the kind of work it asks for. The task may be in any "
    "language. Read what is being asked, not the words it uses: 'the header no longer looks "
    "right' is UI work, not debugging. Kinds:\n"
    + "\n".join(f"- {k}: {v}" for k, v in _KIND_HELP.items())
    + "\nComplexity is how hard the work is, not how long the text is: trivial, low, medium "
    "or high. Confidence is how sure you are of the kind, from 0 to 1."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(_KIND_HELP)},
        "complexity": {"type": "string", "enum": list(COMPLEXITIES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["kind", "complexity", "confidence", "reason"],
    "additionalProperties": False,
}


def ask_haiku(
    system_prompt: str,
    schema: Mapping[str, object],
    text: str,
    *,
    runner: Runner = subprocess.run,
    claude_bin: str = "claude",
    cwd: str | Path | None = None,
) -> tuple[dict | None, float, str]:
    """One question to Haiku with a JSON schema and no tools: (answer, cost, why there is none).
    Never raises; a failure is (None, cost so far, the reason)."""
    argv = [
        claude_bin, "-p",
        "--model", "haiku",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema),
    ]
    try:
        if cwd:
            # A neutral directory, so no project's instructions ride along; it may
            # not exist yet on a machine that has never stored anything.
            Path(cwd).mkdir(parents=True, exist_ok=True)
        proc = runner(
            argv,
            input=text,
            capture_output=True,
            text=True,
            timeout=CLASSIFIER_TIMEOUT_S,
            cwd=str(cwd) if cwd else None,
            env=_quiet_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, 0.0, f"the model did not run: {exc}"
    try:
        reply = json.loads(proc.stdout or "")
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return None, 0.0, f"the model returned no JSON: {detail}"
    if not isinstance(reply, dict):
        return None, 0.0, "the model returned no JSON object"
    cost = reply.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    out = reply.get("structured_output")
    if reply.get("is_error") or not isinstance(out, dict):
        return None, cost, "the model gave no decision"
    return out, cost, ""


def by_model(text: str, **haiku_kwargs) -> Classification:
    """Haiku's reading of the request. Never raises: a failure is the default kind."""
    out, cost, failure = ask_haiku(SYSTEM_PROMPT, SCHEMA, text, **haiku_kwargs)
    if out is None:
        return Classification(DEFAULT_KIND, reason=failure.replace("the model", "the classifier"), cost_usd=cost)
    kind, complexity = out.get("kind"), out.get("complexity")
    confidence = out.get("confidence")
    if kind not in _KIND_HELP or complexity not in COMPLEXITIES:
        return Classification(DEFAULT_KIND, reason="the classifier answered outside the schema", cost_usd=cost)
    if not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
        return Classification(
            DEFAULT_KIND,
            complexity=str(complexity),
            reason=f"the classifier was unsure ({confidence}); it guessed {kind}",
            cost_usd=cost,
        )
    return Classification(
        str(kind), str(complexity), "model", str(out.get("reason") or "")[:200], cost
    )


def classify(text: str, *, use_model: bool = True, **model_kwargs) -> Classification:
    ruled = by_rules(text)
    if ruled is not None:
        return ruled
    if not use_model:
        return Classification(DEFAULT_KIND, reason="no rule matched and the model was not asked")
    return by_model(text, **model_kwargs)


def _quiet_env() -> dict[str, str]:
    env = dict(os.environ)
    # A worker or a classifier call must not record itself as a task.
    env["CAUCE_HOOKS_OFF"] = "1"
    return env
