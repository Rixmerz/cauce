"""T0 and T1 of the "lexicon + references" idea, on what real workers were sent and read.

Reads the `tap-*.jsonl` logs `collect.py` writes. Every number about a brief is counted by the
real tokenizer: the CLI is asked once with the text and once with a single character, and the
first request's context (input, cache read and cache write) is told apart (`count`, cached in
`counts.json`, a fraction of a cent each on Haiku). Nothing else calls a model.

    python bench/tokens/analyze.py bench/tokens/tap-*.jsonl

T0: what share of an attempt's input, and of its cost, the brief is. The brief is in every
request of the attempt, so its tokens are paid once per request: written to the cache on the
first (an hour's write is 2x the base price) and read from it on the rest (0.1x).

T1: the same brief under each layer, as it would be sent:
- refs: what the design marks referable (notes, primer, dead ends, earlier attempts, a check's
  output, the session's thread) as one line per item with an id; a fetch tool's schema is added to
  every request. Two bounds: no fetch, and one fetch of everything (a request more, plus the text).
- dedup: the brief without what it says twice (a criterion that copies a ledger row above it, a
  note's title that is the start of its own text, a list line said earlier), nothing left out.
- lexicon: phrases of cauce's own text replaced by one-word aliases, the definitions in every
  request. Scored per request (an alias pays in a request only for what that request repeats) and
  as the design scored it (by its frequency in the whole corpus), to show the difference.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
COUNTS = HERE / "counts.json"
REAL = "/opt/node22/bin/claude"
WRITE, READ, OUT = 2.0, 0.1, 5.0  # an hour's cache write, a cache read, output: multiples of the base input price

#: Where a brief's sections begin, and whether the design lets a reference replace them.
#: Protected: the request, its criteria, constraints, a fix's rows, the verifier's blockers, and
#: cauce's instructions and result contract (stable text the lexicon would touch instead).
SECTIONS = [
    (r"^Task \(", "request", False),
    (r"^Check this change\. The request it answers", "request", False),
    (r"^The request's criteria, in the person's words", "criteria", False),
    (r"^Out of scope, as the request says", "criteria", False),
    (r"^Constraints this project keeps", "constraints", False),
    (r"^The request is a fix of the ledger rows", "ledger", False),
    (r"^The previous check found these", "blockers", False),
    (r"^What this project's notes say about it", "notes", True),
    (r"^How earlier workers of this kind of work here got oriented", "primer", True),
    (r"^Fixes already tried against similar problems", "dead_ends", True),
    (r"^Previous attempts on this task", "attempts", True),
    (r"^  What cauce's own check said when it failed", "check_output", True),
    (r"^  The output that failed the last attempt", "check_output", True),
    (r"^Calls failing in that session|^What the session .*asked|^The session's thread", "thread", True),
    (r"^When you are done, end your reply with exactly one fenced block", "contract", False),
]
_STARTS = re.compile("|".join(f"(?P<s{i}>{p})" for i, (p, _, _) in enumerate(SECTIONS)), re.MULTILINE)
#: What a worker would be given to fetch a reference: one MCP tool, in every request's tool list.
FETCH_TOOL = json.dumps({"name": "mcp__cauce__ref", "description": "The full text of an item the brief names by its "
                         "id in square brackets ([N3], [D1], [A2], [C1]). Call it only when the line shown is not "
                         "enough for the task.", "input_schema": {"type": "object", "properties": {"ids": {
                             "type": "array", "items": {"type": "string"}, "description": "the ids to read"}},
                             "required": ["ids"]}})
REF_NOTE = ("Lines marked with an id in square brackets are summaries of kept items; read one in full with "
            "`mcp__cauce__ref` when its line is not enough.")


def _counts() -> dict[str, int]:
    return json.loads(COUNTS.read_text()) if COUNTS.exists() else {}


#: Said to the counting call, and to its one-character baseline alike, so it cancels out: the
#: count needs only the first request's input, and a model that answers a brief takes minutes.
HUSH = "Whatever the user message says, reply with exactly one word: ok."


def _context(text: str) -> int:
    done = subprocess.run([REAL, "-p", "--model", "haiku", "--max-turns", "1", "--tools", "", "--output-format",
                           "json", "--no-session-persistence", "--strict-mcp-config", "--append-system-prompt", HUSH],
                          input=text, capture_output=True, text=True, timeout=300, check=False)
    usage = json.loads(done.stdout)["usage"]
    first = (usage.get("iterations") or [usage])[0]
    return sum(int(first.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens"))


def precount(texts: list[str], memo: dict[str, int], parallel: int = 8) -> None:
    """Counts every text not counted yet, several at once."""
    from concurrent.futures import ThreadPoolExecutor
    if "." not in memo:
        memo["."] = _context(".")
    todo = {hashlib.sha256(t.encode()).hexdigest(): t for t in texts if t}
    todo = {k: t for k, t in todo.items() if k not in memo}
    with ThreadPoolExecutor(parallel) as pool:
        for k, n in zip(todo, pool.map(_context, todo.values()), strict=True):
            memo[k] = max(0, n - memo["."])
            COUNTS.write_text(json.dumps(memo))


def count(text: str, memo: dict[str, int]) -> int:
    """The tokens `text` takes, by the real tokenizer: its context against one character's."""
    if not text:
        return 0
    key = hashlib.sha256(text.encode()).hexdigest()
    if key not in memo:
        if "." not in memo:
            memo["."] = _context(".")
        memo[key] = max(0, _context(text) - memo["."])
        COUNTS.write_text(json.dumps(memo))
    return memo[key]


def sections(text: str) -> list[tuple[str, bool, str]]:
    """The brief cut where each known section begins: (name, referable, text)."""
    marks = [(m.start(), next(SECTIONS[int(k[1:])] for k, v in m.groupdict().items() if v)) for m in
             _STARTS.finditer(text)]
    out = []
    if not marks or marks[0][0] > 0:
        out.append(("other", False, text[:marks[0][0] if marks else len(text)]))
    for i, (at, (_, name, ref)) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((name, ref, text[at:end]))
    return out


def _one_line(text: str, chars: int = 70) -> str:
    text = " ".join(text.split())
    if len(text) <= chars:
        return text
    cut = text[:chars].rsplit(" ", 1)[0]
    return cut + "…"


def referenced(text: str) -> tuple[str, str]:
    """The brief with every referable section as one line per item with an id, and the text the
    references stand for (what one fetch of everything brings back)."""
    kept, stood_for, n = [], [], Counter()
    for name, ref, body in sections(text):
        if not ref:
            kept.append(body)
            continue
        stood_for.append(body)
        tag = {"notes": "N", "primer": "P", "dead_ends": "D", "attempts": "A", "check_output": "C",
               "thread": "T"}[name]
        lines = body.rstrip("\n").splitlines()
        items = [ln for ln in lines[1:] if ln.lstrip().startswith(("- ", "attempt "))]
        head = _one_line(lines[0], 60)
        if name == "check_output" or not items:
            n[tag] += 1
            kept.append(f"{head} [{tag}{n[tag]}] ({len(lines)} lines)\n\n")
            continue
        out = [head]
        for item in items:
            n[tag] += 1
            out.append(f"- [{tag}{n[tag]}] {_one_line(item.lstrip('- '))}")
        kept.append("\n".join(out) + "\n\n")
    if not stood_for:
        return text, ""
    return REF_NOTE + "\n\n" + "".join(kept), "".join(stood_for)


_ROW = re.compile(r"^- (F\d+) (.+)$", re.MULTILINE)
_CRIT = re.compile(r"^(S\d+): (.+)$", re.MULTILINE)
_TITLED = re.compile(r"^(- #\d+ \[[^\]]+\] )(.+?)…: (.+)$", re.MULTILINE)


def deduped(text: str) -> str:
    """The brief without what it says twice, nothing left out and nothing behind a reference:
    a criterion that is a row already quoted in the brief points at the row; a note's title that
    is the start of its own text is left out; a list line said earlier is said once."""
    rows = {m.group(2)[:120]: m.group(1) for m in _ROW.finditer(text)}

    def crit(m: re.Match) -> str:
        body = m.group(2)
        for row, fid in rows.items():
            if body.startswith(fid + " ") and row[:80] in body:
                return f"{m.group(1)}: {fid}, the ledger row above, verbatim"
        return m.group(0)

    text = _CRIT.sub(crit, text)
    text = _TITLED.sub(lambda m: m.group(1) + m.group(3) if m.group(3).startswith(m.group(2).rstrip()) else
                       m.group(0), text)
    out, seen = [], set()
    for line in text.splitlines(keepends=True):
        key = line.strip()
        if key.startswith("- ") and len(key) > 20 and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "".join(out)


def cauce_text(text: str) -> str:
    """What cauce wrote in a brief, without the person's request, the files, or a check's output:
    what a lexicon may touch."""
    return "\n".join(body for name, _, body in sections(text)
                     if name in ("contract", "other", "notes", "primer", "dead_ends", "constraints") or
                     name == "attempts")


_WORD = re.compile(r"[A-Za-z][A-Za-z'`-]*")


def ngrams(text: str, lo: int = 4, hi: int = 14) -> Counter:
    words = _WORD.findall(text)
    got: Counter = Counter()
    for n in range(lo, hi + 1):
        for i in range(len(words) - n + 1):
            got[" ".join(words[i:i + n])] += 1
    return got


def lexicon(calls: list[dict], tok_per_char: float) -> dict:
    """The best lexicon each way of scoring finds, and what it saves per request under the true
    rule: an alias saves (phrase - 1) tokens per use in a request, and its definition (phrase + 3)
    is in every request."""
    per = [ngrams(cauce_text(c["stdin"] + "\n" + c["system"])) for c in calls]
    corpus: Counter = Counter()
    for got in per:
        corpus.update(got)

    def toks(phrase: str) -> float:
        return len(phrase) * tok_per_char

    designed = {p for p, f in corpus.items() if (toks(p) - 1) * f - (toks(p) + 3) > 0 and toks(p) > 3}
    true_gain: dict[str, float] = {}
    for p in {p for got in per for p, c in got.items() if c >= 2}:
        gain = sum((toks(p) - 1) * got.get(p, 0) - (toks(p) + 3) for got in per) / len(per)
        if gain > 0:
            true_gain[p] = gain

    def pick(cands: set[str]) -> list[str]:
        # longest first, never two that overlap in words: a greedy cover, as a real lexicon would be built
        chosen: list[str] = []
        for p in sorted(cands, key=lambda p: -toks(p) * corpus[p]):
            if not any(p in q or q in p for q in chosen):
                chosen.append(p)
            if len(chosen) >= 200:
                break
        return chosen

    def per_request(chosen: list[str]) -> list[float]:
        return [sum((toks(p) - 1) * got.get(p, 0) for p in chosen) - sum(toks(p) + 3 for p in chosen)
                for got in per]

    a, b = pick(designed), pick(set(true_gain))
    return {"designed_entries": len(a), "designed_net_per_request": per_request(a),
            "true_entries": len(b), "true_net_per_request": per_request(b), "true_top": b[:8]}


def repeated_share(text: str, n: int = 8) -> float:
    """The share of a brief's words that repeat, within the same brief, a run of `n` words said
    earlier in it: what citing what was already said would save, with no alias and no fetch."""
    words = _WORD.findall(text)
    seen: set[tuple[str, ...]] = set()
    covered = [False] * len(words)
    for i in range(len(words) - n + 1):
        gram = tuple(words[i:i + n])
        if gram in seen:
            for j in range(i, i + n):
                covered[j] = True
        seen.add(gram)
    return sum(covered) / len(words) if words else 0.0


def kind_of(row: dict) -> str:
    s = row["stdin"]
    if "--resume" in row["argv"]:
        return "continued"
    if s.startswith("Check this change"):
        return "verifier"
    if s.startswith("Task ("):
        return "later attempt" if "Previous attempts on this task" in s else "first attempt"
    return "service"


def usage_of(row: dict) -> dict[str, int]:
    got = Counter()
    for m in (row.get("modelUsage") or {}).values():
        got["in"] += int(m.get("inputTokens") or 0)
        got["cw"] += int(m.get("cacheCreationInputTokens") or 0)
        got["cr"] += int(m.get("cacheReadInputTokens") or 0)
        got["out"] += int(m.get("outputTokens") or 0)
    return dict(got)


def units(u: dict[str, int]) -> float:
    """An attempt's cost in base input prices."""
    return u["in"] + WRITE * u["cw"] + READ * u["cr"] + OUT * u["out"]


def brief_units(tokens: int, requests: int) -> float:
    return tokens * (WRITE + READ * max(0, requests - 1))


def main(paths: list[str]) -> None:
    rows = [json.loads(x) for p in paths for x in Path(p).read_text().splitlines() if x.strip()]
    calls = [r for r in rows if r.get("modelUsage") and kind_of(r) != "service"]
    memo = _counts()
    texts = [FETCH_TOOL]
    for r in calls:
        refs, stood_for = referenced(r["stdin"])
        texts += [r["stdin"], r["system"], refs if stood_for else "", stood_for, deduped(r["stdin"])]
    precount(texts, memo)
    fetch_tool = count(FETCH_TOOL, memo)
    table, by_kind = [], defaultdict(list)
    for r in calls:
        u, req = usage_of(r), max(1, int(r.get("num_turns") or 1))
        prompt = r["stdin"]
        b = count(prompt, memo)
        s = count(r["system"], memo)
        refs, stood_for = referenced(prompt)
        rb = count(refs, memo) if stood_for else b
        full = count(stood_for, memo) if stood_for else 0
        dd = count(deduped(prompt), memo)
        ctx = u["in"] + u["cw"] + u["cr"]
        total = units(u)
        # refs, no fetch: a shorter brief, plus the tool's schema in every request
        save0 = brief_units(b - rb, req) - (brief_units(fetch_tool, req) if stood_for else 0)
        # refs, one fetch of everything: one more request at the attempt's mean context, and the text again
        extra = (ctx / req) * READ + full * (WRITE + READ * max(0, req - 2)) if stood_for else 0
        row = {"kind": kind_of(r), "requests": req, "brief": b, "system": s, "ctx": ctx,
               "token_share": b * req / ctx if ctx else 0, "cost_share": brief_units(b, req) / total if total else 0,
               "refs_saved_tokens": b - rb, "refs_save_best": save0 / total, "refs_save_worst": (save0 - extra) / total,
               "referable_share": (b - rb) / b if b else 0, "repeated_share": repeated_share(prompt),
               "dedup_saved_tokens": b - dd, "dedup_save": brief_units(b - dd, req) / total,
               "total_units": total, "cost": r.get("total_cost_usd")}
        table.append(row)
        by_kind[row["kind"]].append(row)
    tpc = sum(t["brief"] for t in table) / max(1, sum(len(r["stdin"]) for r in calls))
    lex = lexicon(calls, tpc)

    def agg(rows: list[dict]) -> dict:
        total = sum(r["total_units"] for r in rows)
        return {"n": len(rows), "requests_p50": median(r["requests"] for r in rows),
                "brief_tokens_p50": median(r["brief"] for r in rows),
                "ctx_tokens_p50": median(r["ctx"] for r in rows),
                "brief_token_share_p50": round(median(r["token_share"] for r in rows), 4),
                "brief_cost_share_p50": round(median(r["cost_share"] for r in rows), 4),
                "brief_cost_share_pooled": round(sum(r["cost_share"] * r["total_units"] for r in rows) / total, 4),
                "referable_share_of_brief_p50": round(median(r["referable_share"] for r in rows), 3),
                "repeated_within_brief_p50": round(median(r["repeated_share"] for r in rows), 3),
                "dedup_saved_tokens_p50": median(r["dedup_saved_tokens"] for r in rows),
                "dedup_saved_tokens_max": max(r["dedup_saved_tokens"] for r in rows),
                "dedup_save_pooled": round(sum(r["dedup_save"] * r["total_units"] for r in rows) / total, 4),
                "refs_save_best_pooled": round(sum(r["refs_save_best"] * r["total_units"] for r in rows) / total, 4),
                "refs_save_worst_pooled": round(sum(r["refs_save_worst"] * r["total_units"] for r in rows) / total, 4)}

    lex_units = list(lex["true_net_per_request"])
    pooled = sum(t["total_units"] for t in table)
    usages = [usage_of(r) for r in calls]
    where_cost = {
        "output": round(sum(OUT * u["out"] for u in usages) / pooled, 3),
        "final_answer_text": round(sum(r["result_chars"] * tpc * OUT for r in calls) / pooled, 4),
        "appended_system_tokens_p50": median(t["system"] for t in table),
        # its share: at most as the brief is paid (written per attempt), at least read from a cache workers share
        "appended_system_share_max": round(sum(brief_units(t["system"], t["requests"]) for t in table) / pooled, 3),
        "appended_system_share_min": round(sum(t["system"] * READ * t["requests"] for t in table) / pooled, 3)}

    def lex_share(per_request: float) -> float:
        return round(sum(brief_units(int(per_request), t["requests"]) for t in table) / pooled, 4)
    report = {"calls": len(calls), "cost_usd": round(sum(t["cost"] or 0 for t in table), 2),
              "tokens_per_char": round(tpc, 3), "fetch_tool_tokens": fetch_tool, "where_cost_goes": where_cost,
              "all": agg(table), "by_kind": {k: agg(v) for k, v in sorted(by_kind.items())},
              "lexicon": {"designed_entries": lex["designed_entries"],
                          "designed_net_tokens_per_request_p50": median(lex["designed_net_per_request"]),
                          "designed_share_of_cost": lex_share(median(lex["designed_net_per_request"])),
                          "true_entries": lex["true_entries"],
                          "true_net_tokens_per_request_p50": median(lex_units) if lex_units else 0,
                          "true_share_of_cost_max": lex_share(median(lex_units) if lex_units else 0),
                          "true_top_phrases": lex["true_top"]}}
    print(json.dumps(report, indent=1))
    (HERE / "report.json").write_text(json.dumps({"report": report, "calls": table}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
