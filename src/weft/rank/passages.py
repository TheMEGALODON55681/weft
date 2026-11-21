"""Passages: pieces of a page small enough to answer a question, ranked with BM25.

A page's Markdown is cut at headings. Short lines are gathered up to a size, long ones are cut at
sentence ends, and each passage remembers the heading it sits under. Ranking reuses `Bm25` with
the passages of every page as one corpus, so a word that appears in half the passages counts for
little and a rare one for a lot. The page title and the heading count as words of the passage,
because they say what the passage is about.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from weft.rank.bm25 import Bm25, term_offsets, terms

MAX_PASSAGE_CHARS = 700
MIN_PASSAGE_CHARS = 150  # a passage this long stands alone; a shorter one gathers its neighbours
SNIPPET_CHARS = 240
_SNIPPET_LEAD = 40  # context kept before the first match in a snippet
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class Passage:
    text: str
    heading: str | None  # the nearest heading above it


@dataclass(frozen=True, slots=True)
class Hit:
    url: str
    title: str | None
    heading: str | None
    text: str
    score: float  # BM25: unbounded, only comparable within one ranking


def _heading(line: str) -> str | None:
    """The text of an ATX heading line, else None. Plain string work: a regex over a line of
    untrusted text can backtrack for a very long time."""
    if not line.startswith("#"):
        return None
    text = line.lstrip("#")
    if len(line) - len(text) > 6 or not text[:1].isspace():
        return None
    return text.strip().rstrip("#").strip() or None


def _pack(parts: Iterable[str]) -> list[str]:
    """Join consecutive parts with spaces, at most MAX_PASSAGE_CHARS per piece."""
    pieces: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + 1 + len(part) > MAX_PASSAGE_CHARS:
            pieces.append(current)
            current = part
        else:
            current = f"{current} {part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def _fit(line: str) -> list[str]:
    """The line whole if it fits, else runs of sentences, else runs of words, else slices."""
    if len(line) <= MAX_PASSAGE_CHARS:
        return [line]
    parts: list[str] = []
    for sentence in _SENTENCE_END.split(line):
        if len(sentence) <= MAX_PASSAGE_CHARS:
            parts.append(sentence)
            continue
        for word in sentence.split():
            parts.extend(
                word[i : i + MAX_PASSAGE_CHARS] for i in range(0, len(word), MAX_PASSAGE_CHARS)
            )
    return _pack(parts)


def split_passages(markdown: str) -> list[Passage]:
    out: list[Passage] = []
    heading: str | None = None
    lines: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal lines, size
        if lines:
            out.append(Passage("\n".join(lines), heading))
        lines, size = [], 0

    for raw in markdown.splitlines():
        line = raw.strip()
        if not any(c.isalnum() for c in line):  # blank, a rule, a table separator, a bare fence
            continue
        if (title := _heading(line)) is not None:
            flush()  # the text so far belongs under the old heading
            heading = title
            continue
        for unit in _fit(line):
            if lines and (size >= MIN_PASSAGE_CHARS or size + 1 + len(unit) > MAX_PASSAGE_CHARS):
                flush()
            size += len(unit) + (1 if lines else 0)
            lines.append(unit)
    flush()
    return out


def rank_passages(
    query: str, pages: Iterable[tuple[str, str | None, str]]
) -> tuple[list[Hit], int]:
    """Rank the passages of `pages`, each a (url, title, markdown), against the query.

    Returns the passages that hold at least one query word, best first, and how many passages
    were searched. A passage that appears word for word on several pages counts once, at the
    first page that has it. Equal scores keep the order the pages came in.
    """
    bm25 = Bm25(query)
    seen: set[str] = set()
    corpus: list[tuple[str, str | None, Passage, list[str]]] = []
    for url, title, markdown in pages:
        for passage in split_passages(markdown):
            if passage.text in seen:
                continue
            seen.add(passage.text)
            doc = terms(f"{title or ''}\n{passage.heading or ''}\n{passage.text}")
            bm25.add(doc)
            corpus.append((url, title, passage, doc))
    hits = [
        Hit(url, title, passage.heading, passage.text, score)
        for url, title, passage, doc in corpus
        if (score := bm25.score(doc)) > 0
    ]
    hits.sort(key=lambda h: -h.score)  # stable
    return hits, len(corpus)


def snippet(text: str, query: str, width: int = SNIPPET_CHARS) -> str:
    """The stretch of `text` that holds the most query words: at most `width` characters, cut
    at word boundaries, with an ellipsis where text was left out."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    offsets = term_offsets(flat, set(terms(query)))
    starts = [max(0, o - _SNIPPET_LEAD) for o in offsets] or [0]
    # The first window with the most matches.
    start = max(starts, key=lambda s: sum(s <= o < s + width for o in offsets))
    if start:
        start = flat.rfind(" ", 0, start) + 1  # back to the start of the word
    end = start + width
    if end < len(flat):
        cut = flat.rfind(" ", start, end)
        if cut > start + width // 2:  # a word boundary near the end; else cut the long word
            end = cut
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
