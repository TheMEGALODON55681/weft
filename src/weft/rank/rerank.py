"""Semantic rerank: BM25 picks the candidates, embeddings reorder them.

Off unless the operator turns it on. Each candidate's rank by keywords and its rank by embedding
similarity to the query are fused with reciprocal rank fusion, so neither score has to be put on
the other's scale and a strong keyword hit is not pushed out by a vague semantic one. Only passages
that already matched a query word are candidates: this helps order, not recall, so a passage that
shares no word with the query is still not found.

YAGNI note: no weight between the two rankings. Add one if fusing them equally ever reads wrong.
"""

import math
from dataclasses import replace

from weft.providers import EmbeddingProvider, ProviderError
from weft.rank.passages import Hit

RERANK_CANDIDATES = 50  # the best keyword hits that are embedded
_RRF_K = 60  # the usual constant for reciprocal rank fusion


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(math.sumprod(a, a)) * math.sqrt(math.sumprod(b, b))
    return math.sumprod(a, b) / norm if norm else 0.0


async def rerank(provider: EmbeddingProvider, query: str, hits: list[Hit]) -> list[Hit]:
    """`hits` (best keyword match first) reordered by fused rank, with `score` set to the fused
    score. Raises ProviderError; the caller keeps the keyword order."""
    if not hits:
        return []
    texts = [query, *("\n".join(filter(None, (h.title, h.heading, h.text))) for h in hits)]
    vectors = await provider.embed(texts)
    if len(vectors) != len(texts) or any(len(v) != len(vectors[0]) for v in vectors):
        raise ProviderError("provider_bad_response", "embeddings do not match the inputs")
    query_vector, *passage_vectors = vectors
    similar = [_cosine(query_vector, v) for v in passage_vectors]
    by_meaning = sorted(range(len(hits)), key=lambda i: -similar[i])
    meaning_rank = {i: rank for rank, i in enumerate(by_meaning, 1)}
    fused = [
        replace(h, score=1 / (_RRF_K + i + 1) + 1 / (_RRF_K + meaning_rank[i]))
        for i, h in enumerate(hits)
    ]
    return sorted(fused, key=lambda h: -h.score)  # stable: a tie keeps the keyword order
