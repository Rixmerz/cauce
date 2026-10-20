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
    return clip(text, limit, one_line=True)


#: A sentence end: its mark, then a space (or the text's end).
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")
#: How far back from the limit a sentence end or a word boundary may be and still be the cut.
_SENTENCE_REACH = 0.6
_WORD_REACH = 0.5
_OPENERS = {"[": "]", "(": ")"}


def clip(text: str, limit: int, *, one_line: bool = False) -> str:
    """`text` in at most `limit` characters, cut where a reader expects it: after a
    sentence that ends close to the limit, else at a word boundary, never inside a
    bracketed part (`[claude-…]`, `(line 197)`) begun near the cut, and marked "…".
    `one_line` collapses its whitespace first. The one place reports clip text."""
    s = " ".join(str(text or "").split()) if one_line else str(text or "")
    if len(s) <= limit:
        return s
    if limit < 2:
        return "…"[:limit]
    room = limit - 1  # the "…" takes the last character
    window = s[: room + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() <= room - 1]
    if ends and ends[-1] >= room * _SENTENCE_REACH:
        cut, sentence = ends[-1], True
    else:
        spaces = [i for i, c in enumerate(window) if c.isspace()]
        cut = spaces[-1] if spaces and spaces[-1] >= room * _WORD_REACH else room
        sentence = False
    cut = _outside_brackets(s, cut, floor=int(room * _WORD_REACH))
    head = s[:cut].rstrip()
    if sentence and len(head) <= room - 1:
        return head + " …"
    return head.rstrip(" ,;:-–—") + "…"


def _outside_brackets(s: str, cut: int, *, floor: int) -> int:
    """`cut`, moved back before a bracket still open there when it opened past `floor`."""
    depth: list[int] = []
    for i, c in enumerate(s[:cut]):
        if c in _OPENERS:
            depth.append(i)
        elif depth and c == _OPENERS[s[depth[-1]]]:
            depth.pop()
    if depth and depth[-1] >= floor:
        return depth[-1]
    return cut


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
