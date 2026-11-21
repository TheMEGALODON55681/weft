"""Duplicate fingerprints, BM25 and the goal scores built on it."""

from weft.frontier.dedupe import (
    NEAR_BITS,
    content_hash,
    distance,
    from_signed,
    simhash,
    to_signed,
)
from weft.frontier.score import link_score, relevance
from weft.rank.bm25 import Bm25, stem, terms

ARTICLE = (
    "Release notes for version four. The importer now reads spreadsheets with merged cells, the "
    "exporter keeps column widths, and the search box remembers the last ten queries. Several "
    "fixes landed as well: dates no longer shift by a day, and printing a long table no longer "
    "cuts rows."
)


def test_content_hash_ignores_spacing_and_case() -> None:
    assert content_hash("Hello   World\n") == content_hash("hello world")
    assert content_hash("hello world") != content_hash("hello there")


def test_a_one_word_change_is_a_near_duplicate() -> None:
    a = simhash(ARTICLE + " Published on 3 March.")
    b = simhash(ARTICLE + " Published on 4 March.")
    assert distance(a, b) <= NEAR_BITS


def test_unrelated_text_is_far_apart() -> None:
    other = (
        "The garden club meets on the first Saturday of every month in the community hall. "
        "Members bring seedlings to swap, and the committee reports on the orchard and ponds."
    )
    assert distance(simhash(ARTICLE), simhash(other)) > 3 * NEAR_BITS


def test_simhash_survives_sqlite_integers() -> None:
    for h in (0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1):
        s = to_signed(h)
        assert -(1 << 63) <= s < 1 << 63
        assert from_signed(s) == h


def test_stemming_meets_the_obvious_forms() -> None:
    assert stem("pricing") == stem("price") == stem("prices")
    assert stem("jobs") == "job"
    assert stem("salaries") == stem("salary")
    assert stem("is") == "is"  # too short to strip


def test_single_letters_are_not_terms() -> None:
    assert terms("q plan x") == ["plan"]


def test_goal_words_that_describe_the_task_are_dropped() -> None:
    assert terms("Find every pricing page") == [stem("pricing")]


def test_words_outside_ascii_stay_whole() -> None:
    """An ASCII-only tokenizer splits 'für' into single letters and drops them, so a page in
    German, French or Russian would have no searchable terms."""
    assert terms("Größe für Küche") == [stem("größe"), "für", stem("küche")]
    assert terms("Цена подписки") == ["цена", stem("подписки")]
    # Lowering the dotted capital I gives 'i' plus a combining mark. The word must not split there.
    assert terms("İstanbul") == ["i̇stanbul"]
    goal = Bm25("küche")
    doc = terms("Die Küche ist groß")
    goal.add(doc)
    assert goal.score(doc) > 0


def test_bm25_prefers_the_page_about_the_goal() -> None:
    goal = Bm25("team plan pricing")
    on_topic = terms("Pricing for the team plan. The team plan costs twelve dollars.")
    off_topic = terms("The garden club meets every month in the hall to swap seedlings.")
    goal.add(on_topic)
    goal.add(off_topic)
    assert goal.score(on_topic) > goal.score(off_topic) == 0


def test_relevance_is_between_zero_and_one() -> None:
    goal = Bm25("pricing")
    r = relevance(goal, terms("pricing " * 50))
    assert 0 < r < 1
    assert relevance(goal, terms("nothing relevant here")) == 0


def test_link_score_uses_anchor_url_and_parent() -> None:
    goal = Bm25("pricing plans")
    plain = link_score(goal, "About us", "http://a.test/about", 0.0, 1)
    by_anchor = link_score(goal, "Pricing", "http://a.test/x", 0.0, 1)
    by_url = link_score(goal, "Click", "http://a.test/pricing-plans", 0.0, 1)
    by_parent = link_score(goal, "Click", "http://a.test/x", 0.9, 1)
    assert plain == 0
    assert by_anchor > 0 and by_url > 0 and by_parent > 0
    assert link_score(goal, "Pricing plans", "http://a.test/pricing-plans", 1.0, 1) == 1.0


def test_without_a_goal_links_score_by_depth() -> None:
    goal = Bm25("")
    assert link_score(goal, "Pricing", "http://a.test/p", 0.9, 1) > link_score(
        goal, "Pricing", "http://a.test/p", 0.9, 2
    )
