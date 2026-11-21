"""Embedding rerank with a fake embedder. No provider, no network."""

import math

import pytest

from weft.providers import ProviderError
from weft.rank.passages import Hit
from weft.rank.rerank import rerank


def hit(name: str, score: float) -> Hit:
    return Hit(f"http://a.test/{name}", f"Title {name}", None, f"text of {name}", score)


class FakeEmbedder:
    """The query and each passage get the vector in `vectors`, looked up by a word in the text."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [next(v for k, v in self.vectors.items() if k in t) for t in texts]


# Keyword order A, B, C. The embedder finds C closest to the query, then A, then B.
VECTORS = {"query": [1.0, 0.0], "of A": [0.8, 0.6], "of B": [0.0, 1.0], "of C": [1.0, 0.1]}


async def test_the_embeddings_lift_a_passage_without_displacing_a_strong_keyword_hit() -> None:
    hits = [hit("A", 9.0), hit("B", 5.0), hit("C", 2.0)]
    out = await rerank(FakeEmbedder(VECTORS), "query", hits)
    # Fused by rank: A is first by keywords and second by meaning, C the reverse. A has the
    # better pair of ranks, C overtakes B, and B (weak at both) is last.
    assert [h.url.rsplit("/", 1)[1] for h in out] == ["A", "C", "B"]
    assert out[0].score > out[1].score > out[2].score > 0
    assert out[0].text == "text of A" and out[0].title == "Title A"  # nothing else changed


async def test_the_query_and_all_passages_go_in_one_call_with_their_context() -> None:
    fake = FakeEmbedder(VECTORS)
    await rerank(fake, "query", [Hit("http://a.test/A", "Plans", "Team", "text of A", 1.0)])
    assert fake.calls == [["query", "Plans\nTeam\ntext of A"]]


async def test_nothing_to_rerank_makes_no_call() -> None:
    fake = FakeEmbedder(VECTORS)
    assert await rerank(fake, "query", []) == []
    assert fake.calls == []


async def test_a_zero_vector_scores_zero_instead_of_dividing_by_it() -> None:
    """A has no direction, so it is the least similar. Counted as similar (1.0) it would take
    first place by meaning and push B ahead of C."""
    fake = FakeEmbedder(
        {"query": [1.0, 0.0], "of A": [0.0, 0.0], "of B": [0.5, 0.866], "of C": [0.9, 0.436]}
    )
    out = await rerank(fake, "query", [hit("A", 3.0), hit("B", 2.0), hit("C", 1.0)])
    assert [h.url.rsplit("/", 1)[1] for h in out] == ["A", "C", "B"]
    assert all(math.isfinite(h.score) for h in out)


async def test_the_fused_score_is_the_sum_of_two_reciprocal_ranks() -> None:
    out = await rerank(
        FakeEmbedder(VECTORS), "query", [hit("A", 3.0), hit("B", 2.0), hit("C", 1.0)]
    )
    # Keyword ranks A1 B2 C3. By meaning C1 A2 B3.
    assert [(h.url.rsplit("/", 1)[1], h.score) for h in out] == [
        ("A", pytest.approx(1 / 61 + 1 / 62)),
        ("C", pytest.approx(1 / 63 + 1 / 61)),
        ("B", pytest.approx(1 / 62 + 1 / 63)),
    ]


@pytest.mark.parametrize(
    "vectors",
    [
        {"query": [1.0, 0.0], "of A": [1.0, 0.0, 0.0], "of B": [1.0, 0.0]},  # a different size
        {"query": [1.0, 0.0]},  # an embedder that returns one vector for three texts
    ],
)
async def test_an_embedder_that_misbehaves_is_a_provider_error(
    vectors: dict[str, list[float]],
) -> None:
    class Odd:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            if "of B" not in vectors:
                return [vectors["query"]]
            return [next(v for k, v in vectors.items() if k in t) for t in texts]

    with pytest.raises(ProviderError) as e:
        await rerank(Odd(), "query", [hit("A", 3.0), hit("B", 2.0)])
    assert e.value.code == "provider_bad_response"
