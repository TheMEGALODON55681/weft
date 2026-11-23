"""Passages: pieces of a page small enough to answer a question, ranked with BM25.

A page's Markdown is read as blocks: a heading, a paragraph (its wrapped lines joined into one
line), a list item, a table row, a block of code. Blocks are gathered up to a size and a passage
closes only between two blocks, so it ends where a block ends. A block longer than a passage is
cut at sentence ends. Each passage remembers the heading it sits under. Ranking reuses `Bm25`
with the passages of every page as one corpus, so a word that appears in half the passages counts
for little and a rare one for a lot. The page title and the heading count as words of the
passage, because they say what the passage is about.

`diverse` then picks from the ranked hits so that one section cannot fill the top of the list.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from weft.frontier.dedupe import MIN_WORDS, content_hash, distance, simhash, words
from weft.rank.bm25 import Bm25, term_offsets, terms

MAX_PASSAGE_CHARS = 700
MIN_PASSAGE_CHARS = 150  # a passage this long stands alone; a shorter one gathers its neighbours
SNIPPET_CHARS = 240
_SNIPPET_LEAD = 40  # context kept before the first match in a snippet
MAX_HITS_PER_SECTION = 2  # a section is a heading on one page
# Simhashes this close or closer count as one passage. A page uses 6 bits (`dedupe.NEAR_BITS`), but
# a page is long and a passage is a paragraph, which moves further when a word changes. On 107
# paragraphs of 30 to 110 words from the project's own docs, 6 bits caught 38% of one-word edits,
# 10 bits 90% and 12 bits all of them (and 85% of two-word edits). The closest pair of unrelated
# paragraphs was 18 bits apart, and none of 5,671 pairs fell within 12.
NEAR_PASSAGE_BITS = 12

# A sentence ends at ".", "!" or "?", then any closing quote, bracket or emphasis mark, then
# white space. Every character class here is disjoint from the next, so a match is linear.
_CLOSERS = "\"')]*_`" + chr(0x201D) + chr(0x2019)  # and the curly closing quotes
_SENTENCE_END = re.compile(rf"[.!?][{re.escape(_CLOSERS)}]*\s+")
_ORDERED_ITEM = re.compile(r"\d{1,3}[.)]\s")  # three digits: "2023. It" is not an item


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


def _is_fence(line: str) -> bool:
    # A backtick fence may not have a backtick in its info string: "```x```" is inline code.
    return line.startswith("~~~") or (line.startswith("```") and "`" not in line[3:])


def _starts_item(line: str, in_paragraph: bool) -> bool:
    """A list item starts at this line. A bullet always starts one. A number does only when no
    paragraph is open or when it is 1, as in CommonMark: a wrapped line that happens to begin with
    "3. " is the paragraph's text."""
    if line.startswith(("- ", "* ", "+ ")):
        return True
    m = _ORDERED_ITEM.match(line)
    return m is not None and (not in_paragraph or int(m.group()[:-2]) == 1)


def _has_text(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _blocks(markdown: str) -> list[tuple[str, str]]:
    """The Markdown as (kind, text) blocks in order. Kind is "heading", "text" (a paragraph or
    list item with its wrapped lines joined by spaces, or one table row) or "code" (a fenced
    block, one line per line of code). A line with no letter or digit is dropped, and so is a
    fence itself. Everything is plain string work, so no input makes it slow."""
    out: list[tuple[str, str]] = []
    run: list[str] = []  # the lines of the paragraph or list item being read
    is_item = False
    code: list[str] | None = None  # the lines of the fenced block being read
    fence = ""

    def end_run() -> None:
        nonlocal is_item
        if run:
            out.append(("text", " ".join(run)))
            run.clear()
        is_item = False

    def end_code() -> None:
        nonlocal code
        if code is not None and _has_text("".join(code)):
            out.append(("code", "\n".join(code)))
        code = None

    for raw in markdown.splitlines():
        line = raw.strip()
        if code is not None:
            if line.startswith(fence):
                end_code()
            elif line:
                code.append(line)
        elif _is_fence(line):
            end_run()
            code, fence = [], line[:3]
        elif not _has_text(line):  # blank, a rule, a table separator
            end_run()
        elif (title := _heading(line)) is not None:
            end_run()
            out.append(("heading", title))
        elif line.startswith("|"):  # a table row is a block of its own
            end_run()
            out.append(("text", line))
        else:
            if _starts_item(line, in_paragraph=bool(run) and not is_item):
                end_run()
                is_item = True
            run.append(line)
    end_run()
    end_code()  # a fence that is never closed runs to the end
    return out


def _sentences(text: str) -> list[str]:
    """`text` cut after each sentence end. A full stop before a lowercase word is not one, since
    it is "e.g." or "etc." or the like. A question or exclamation mark always is."""
    parts: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        if m.group()[0] == "." and text[m.end() : m.end() + 1].islower():
            continue
        parts.append(text[start : m.end()].strip())
        start = m.end()
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _pack(parts: Iterable[str], sep: str = " ") -> list[str]:
    """Join consecutive parts with `sep`, at most MAX_PASSAGE_CHARS per piece."""
    pieces: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(sep) + len(part) > MAX_PASSAGE_CHARS:
            pieces.append(current)
            current = part
        else:
            current = f"{current}{sep}{part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def _fit(text: str) -> list[str]:
    """The text whole if it fits, else runs of sentences, else runs of words, else slices. Only a
    sentence longer than a passage is ever cut inside."""
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]
    parts: list[str] = []
    for sentence in _sentences(text):
        if len(sentence) <= MAX_PASSAGE_CHARS:
            parts.append(sentence)
            continue
        for word in sentence.split():
            parts.extend(
                word[i : i + MAX_PASSAGE_CHARS] for i in range(0, len(word), MAX_PASSAGE_CHARS)
            )
    return _pack(parts)


def _fit_code(text: str) -> list[str]:
    """Code whole if it fits, else cut between its lines (a very long line is cut as text)."""
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]
    return _pack([piece for line in text.split("\n") for piece in _fit(line)], "\n")


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

    for kind, text in _blocks(markdown):
        if kind == "heading":
            flush()  # the text so far belongs under the old heading
            heading = text
            continue
        for unit in _fit_code(text) if kind == "code" else _fit(text):
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


def diverse(hits: list[Hit], limit: int) -> list[Hit]:
    """The first `limit` of `hits`, best first, that each add something. At most
    MAX_HITS_PER_SECTION come from one section (a heading on one page), so a long section
    cannot fill the top of the list, and a hit is left out if an earlier one has the same words
    or is a near copy of it. The better-ranked copy is the one kept.

    A passage of at least MIN_WORDS words is a near copy when its simhash is within
    NEAR_PASSAGE_BITS of another's. A shorter one is too noisy for that, and only an exact
    repeat of its words counts, ignoring case and spacing.
    """
    kept: list[Hit] = []
    per_section: Counter[tuple[str, str | None]] = Counter()
    seen: list[tuple[str, int | None]] = []  # (hash of the words, simhash if long enough)
    for h in hits:
        if len(kept) >= limit:
            break
        section = (h.url, h.heading)
        if per_section[section] >= MAX_HITS_PER_SECTION:
            continue
        digest = content_hash(h.text)
        fuzzy = simhash(h.text) if len(words(h.text)) >= MIN_WORDS else None
        if any(
            digest == d
            or (fuzzy is not None and s is not None and distance(fuzzy, s) <= NEAR_PASSAGE_BITS)
            for d, s in seen
        ):
            continue
        per_section[section] += 1
        seen.append((digest, fuzzy))
        kept.append(h)
    return kept


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
