"""Goal scores. A link's priority comes from what is known before fetching it: its anchor text,
the words in its URL, and how relevant the page that linked to it was. A fetched page's
relevance is BM25 of its title and text against the goal, squashed to 0..1.

With no goal, every link scores by depth alone, so the crawl is breadth-first.
"""

import re
from urllib.parse import unquote, urlsplit

from weft.rank.bm25 import Bm25, terms

# Weights of the three signals in a link's score. They sum to 1.
ANCHOR_W, URL_W, PARENT_W = 0.45, 0.25, 0.30
# BM25 is unbounded. score / (score + _SQUASH) maps it to 0..1 with 0.5 at _SQUASH.
_SQUASH = 2.0
_URL_SPLIT = re.compile(r"[/\-_.?=&+]+")


def _overlap(goal: list[str], text_terms: list[str]) -> float:
    """Share of goal terms present in the text."""
    if not goal:
        return 0.0
    present = set(text_terms)
    return sum(t in present for t in goal) / len(goal)


def url_terms(url: str) -> list[str]:
    parts = urlsplit(url)
    return terms(" ".join(_URL_SPLIT.split(unquote(parts.path + " " + parts.query))))


def link_score(goal: Bm25, anchor: str, url: str, parent_relevance: float, depth: int) -> float:
    if not goal.query:
        return 1.0 / (1 + depth)
    return (
        ANCHOR_W * _overlap(goal.query, terms(anchor))
        + URL_W * _overlap(goal.query, url_terms(url))
        + PARENT_W * parent_relevance
    )


def relevance(goal: Bm25, doc_terms: list[str]) -> float:
    """Count the page in the goal's corpus, then score it."""
    goal.add(doc_terms)
    s = goal.score(doc_terms)
    return s / (s + _SQUASH)
