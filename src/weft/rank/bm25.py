"""BM25 over a corpus that grows as a crawl fetches pages.

Only the query's own terms are tracked, so the statistics stay small however large the crawl.
Early in a crawl the corpus is a handful of pages, so IDF is rough; it settles as pages arrive.
"""

import math
import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")
# Words that say what the caller wants done, not what to look for.
_STOP_WORDS = """a all an and any are as at be by each every find for from get how i in is it list
me of on or page pages show the to what where which with"""
_STOP = frozenset(_STOP_WORDS.split())


def stem(word: str) -> str:
    """A deliberately small suffix stripper: plural and common verb endings only. Enough for
    `pricing` to meet `price` and `jobs` to meet `job`."""
    for suffix in ("ings", "ing", "ies", "es", "ed", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def terms(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


class Bm25:
    def __init__(self, query: str, k1: float = 1.2, b: float = 0.75) -> None:
        self.query = sorted(set(terms(query)))
        self._k1, self._b = k1, b
        self._docs = 0
        self._total_len = 0
        self._df: Counter[str] = Counter()

    def add(self, doc_terms: list[str]) -> None:
        """Count a document in the corpus statistics."""
        self._docs += 1
        self._total_len += len(doc_terms)
        present = set(doc_terms)
        self._df.update(t for t in self.query if t in present)

    def score(self, doc_terms: list[str]) -> float:
        if not self.query or not doc_terms or not self._docs:
            return 0.0
        tf = Counter(doc_terms)
        avg = self._total_len / self._docs
        norm = self._k1 * (1 - self._b + self._b * len(doc_terms) / avg)
        total = 0.0
        for t in self.query:
            if tf[t]:
                df = self._df[t]
                idf = math.log(1 + (self._docs - df + 0.5) / (df + 0.5))
                total += idf * tf[t] * (self._k1 + 1) / (tf[t] + norm)
        return total
