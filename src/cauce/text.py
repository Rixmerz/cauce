"""Small text helpers shared by the classifier, the memory and the hooks."""
from __future__ import annotations

import difflib
import re
import unicodedata

_WORD_RE = re.compile(r"[\w#.-]+", re.UNICODE)

#: Words that carry no topic in either language a request arrives in.
STOPWORDS = frozenset((
    # Spanish
    "a", "al", "algo", "ante", "con", "como", "cual", "cuando", "de", "del", "desde", "donde",
    "el", "ella", "en", "entre", "era", "es", "esa", "ese", "eso", "esta", "este", "esto", "hay",
    "la", "las", "le", "les", "lo", "los", "mas", "me", "mi", "mis", "muy", "no", "nos", "o",
    "para", "pero", "por", "porque", "que", "quiero", "se", "si", "sin", "sobre", "su", "sus",
    "tambien", "te", "tengo", "todo", "tu", "un", "una", "uno", "unos", "y", "ya", "favor",
    "necesito", "puedes", "podrias", "hazlo", "haz",
    # English
    "an", "and", "are", "as", "at", "be", "but", "by", "can", "could", "do", "does", "for", "from",
    "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "not",
    "of", "on", "or", "please", "so", "that", "the", "their", "them", "then", "there", "this",
    "to", "us", "was", "we", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your",
))


def fold(text: str) -> str:
    """Lowercase without accents: `migración` and `migracion` are one word."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def words(text: str, *, keep_stopwords: bool = False) -> list[str]:
    found = [w.strip(".-") for w in _WORD_RE.findall(fold(text))]
    return [w for w in found if len(w) > 2 and (keep_stopwords or w not in STOPWORDS)]


def title_of(text: str, limit: int = 120) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def reads_alike(left: str, right: str, threshold: float = 0.85) -> bool:
    """Two summaries that say the same thing.

    Two empty summaries are *not* alike: a worker that reported nothing has not
    repeated an answer, and acting on it would spend the escalation budget on
    missing evidence.
    """
    a, b = " ".join(fold(left).split()), " ".join(fold(right).split())
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold
