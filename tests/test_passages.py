"""Passage splitting, passage-level BM25 and snippets. Pure functions, no network."""

from weft.rank.passages import (
    MAX_PASSAGE_CHARS,
    Hit,
    rank_passages,
    snippet,
    split_passages,
)

PRICING_INTRO = (
    "Our pricing has three plans built around how many people use the product each month, so a "
    "single person and a growing company both pay a fair amount for what they use."
)
TEAM = (
    "The team plan costs twelve dollars per seat each month and adds shared workspaces, "
    "audit logs and priority support for companies that are growing quickly."
)
SECURITY = (
    "Single sign-on and role based access control come with the team plan. Every change is "
    "kept in an audit history for ninety days, and exports are available at any time."
)
MARKDOWN = f"# Pricing\n\n{PRICING_INTRO}\n\n## Team plan\n\n{TEAM}\n\n## Security\n\n{SECURITY}\n"


# --- splitting ------------------------------------------------------------------------------


def test_a_heading_is_the_context_of_the_text_under_it() -> None:
    passages = split_passages(MARKDOWN)
    assert [p.heading for p in passages] == ["Pricing", "Team plan", "Security"]
    assert "twelve dollars" in passages[1].text
    assert "#" not in "".join(p.text for p in passages)  # headings are context, not text


def test_text_before_any_heading_has_none() -> None:
    assert split_passages(f"{TEAM}\n\n# Later\n\n{SECURITY}")[0].heading is None


def test_short_lines_are_gathered_into_one_passage() -> None:
    items = [f"- Item number {n} of the list" for n in range(10)]
    passages = split_passages("\n".join(items))
    assert 1 < len(passages) < len(items)
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)
    assert all(item in "\n".join(p.text for p in passages) for item in items)


def test_a_long_paragraph_is_cut_at_sentence_ends() -> None:
    sentences = [f"Sentence number {n} says one small thing about the garden." for n in range(30)]
    passages = split_passages(" ".join(sentences))
    assert len(passages) > 1
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)
    assert all(p.text.endswith(".") for p in passages)
    for s in sentences:
        assert sum(s in p.text for p in passages) == 1  # none lost, none split, none repeated


def test_a_run_with_no_spaces_is_still_cut_to_size() -> None:
    passages = split_passages("x" * 3000)
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)
    assert sum(len(p.text) for p in passages) == 3000


def test_table_separators_and_rules_are_not_passages() -> None:
    md = "| Plan | Price |\n| --- | --- |\n| Team | 12 |\n\n---\n\n```\n"
    text = "\n".join(p.text for p in split_passages(md))
    assert "---" not in text and "```" not in text
    assert "| Team | 12 |" in text


def test_nothing_in_nothing_out() -> None:
    assert split_passages("") == []
    assert split_passages("\n  \n# \n") == []


def test_seven_hashes_or_no_space_after_them_is_text_and_six_hashes_is_a_heading() -> None:
    (p,) = split_passages("####### seven is not a heading\n#hashtag is plain text too\nbody line")
    assert p.heading is None
    assert "####### seven" in p.text and "#hashtag" in p.text
    (q,) = split_passages("###### six is one\nbody line")
    assert q.heading == "six is one" and q.text == "body line"


def test_a_long_line_is_cut_after_question_and_exclamation_marks_too() -> None:
    line = " ".join(f"{'word ' * 17}{'?' if i % 2 else '!'}" for i in range(10))  # about 900 chars
    passages = split_passages(line)
    assert len(passages) > 1
    assert all(len(p.text) <= MAX_PASSAGE_CHARS and p.text[-1] in "?!" for p in passages)


def test_pieces_are_packed_up_to_the_maximum_and_not_past_it() -> None:
    line = f"{'a' * 349}. {'b' * 349}."  # 701 characters in all: one too many for one passage
    assert [len(p.text) for p in split_passages(line)] == [350, 350]


# --- ranking --------------------------------------------------------------------------------

PRICING = ("http://a.test/pricing", "Pricing", MARKDOWN)
ABOUT = (
    "http://a.test/about",
    "About us",
    "The garden club meets on the first Saturday of every month in the community hall, where "
    "members bring seedlings to swap and the committee reports on the orchard and the ponds.",
)


def test_the_best_passage_wins_and_knows_its_page_and_section() -> None:
    hits, searched = rank_passages("audit logs single sign-on", [ABOUT, PRICING])
    assert searched == 4
    assert (hits[0].url, hits[0].heading) == ("http://a.test/pricing", "Security")
    assert "ninety days" in hits[0].text
    assert hits[0].score > hits[-1].score >= 0
    assert all(isinstance(h, Hit) for h in hits)


def test_a_section_is_ranked_apart_from_the_rest_of_its_page() -> None:
    """The page mentions 'seat' once, in the team section. Only that section should come back
    first, with the others behind it or absent."""
    hits, _ = rank_passages("price per seat", [PRICING])
    assert hits[0].heading == "Team plan"


def test_the_page_title_counts_towards_every_passage_on_it() -> None:
    body = "The team plan costs twelve dollars per seat each month, billed to the workspace owner."
    garden = (
        "The garden club once discussed the pricing of seedlings at its spring meeting, then "
        "moved on to the orchard and the ponds and the rota for watering the beans each week."
    )
    hits, _ = rank_passages(
        "pricing", [("http://a.test/p", "Pricing", body), ("http://a.test/g", "Garden", garden)]
    )
    assert [h.url for h in hits] == ["http://a.test/p", "http://a.test/g"]


def test_a_passage_repeated_on_many_pages_counts_once() -> None:
    # Long enough to stand alone: a short line is gathered with its neighbours and no longer
    # repeats exactly.
    footer = (
        "Copyright Acme. Acme builds an editor for small teams and ships a release every month. "
        "Support is available on weekdays, and the status page lists incidents as they happen."
    )
    pages = [
        (f"http://a.test/{i}", f"Page {i}", f"{footer}\n\nUnique text number {i}.")
        for i in range(3)
    ]
    hits, searched = rank_passages("acme editor", pages)
    assert [h.text for h in hits].count(footer) == 1
    assert searched == 4  # three unique lines, and the footer once


def test_the_heading_counts_as_words_of_the_passage_under_it() -> None:
    page = ("http://a.test/p", "Page", "# Refund policy\n\nWe give money back within thirty days.")
    (hit,) = rank_passages("refund", [page])[0]
    assert hit.heading == "Refund policy"


def test_no_match_no_hits() -> None:
    hits, searched = rank_passages("zebra crossing", [PRICING, ABOUT])
    assert hits == [] and searched == 4
    assert rank_passages("the and of", [PRICING])[0] == []  # no searchable word at all


def test_words_outside_ascii_are_found() -> None:
    page = ("http://a.test/de", "Küche", "Die Küche ist groß und hell, mit Platz für sechs Gäste.")
    assert rank_passages("küche", [ABOUT, page])[0][0].url == "http://a.test/de"


# --- snippets -------------------------------------------------------------------------------

LONG = (
    "Alpha opens the page with a long introduction about nothing in particular, which goes on "
    "for a while so that the interesting part is well out of reach of the first characters. "
    "Somewhere in the middle there is filler, and more filler, and a little more after that. "
    "Near the end the beta release finally ships together with the gamma release candidate."
)


def test_the_snippet_is_the_window_with_the_most_matches() -> None:
    s = snippet(LONG, "beta gamma release")
    assert "beta release" in s and "gamma" in s
    assert s.startswith("…")
    assert "Alpha" not in s


def test_the_snippet_keeps_some_words_from_before_the_first_match() -> None:
    assert "that. Near the end the beta release" in snippet(LONG, "beta gamma release")


def test_the_densest_window_wins_over_the_first_match() -> None:
    text = "alpha " + "filler " * 80 + "beta gamma delta"
    s = snippet(text, "alpha beta gamma delta", width=100)
    assert "beta gamma delta" in s and "alpha" not in s


def test_a_snippet_stays_within_its_width_and_keeps_whole_words() -> None:
    s = snippet(LONG, "beta", width=80)
    assert len(s) <= 80 + 2  # plus the ellipses
    words = set(LONG.replace(".", " ").replace(",", " ").split())
    assert all(w in words for w in s.strip("…").replace(".", " ").replace(",", " ").split())


def test_a_short_passage_is_its_own_snippet() -> None:
    assert snippet("A  short\n passage.", "short") == "A short passage."


def test_a_match_at_the_start_has_no_leading_ellipsis() -> None:
    s = snippet(LONG, "alpha")
    assert s.startswith("Alpha") and s.endswith("…")
