"""Passage splitting, passage-level BM25 and snippets. Pure functions, no network."""

import textwrap
import time
from collections import Counter

import pytest

from weft.rank.passages import (
    MAX_HITS_PER_SECTION,
    MAX_PASSAGE_CHARS,
    Hit,
    _sentences,
    diverse,
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


# --- hard-wrapped source (Phase 8) ----------------------------------------------------------
#
# A docs generator wraps its paragraphs in the source, and the extractor keeps those line breaks
# when the paragraph has inline markup. Splitting line by line then closed a passage wherever a
# line happened to end.

SENTENCES = [
    "Calling cancel on a task asks the event loop to throw an error into the wrapped coroutine.",
    "The coroutine may catch the error and clean up, and it should raise the error again after.",
    "A task that swallows the error looks as though it finished normally to anyone who awaits it.",
    "Cancelling a task that is already done has no effect and returns false to the caller.",
    "Use a timeout when you want the request to cancel itself after a fixed number of seconds.",
    "A group of tasks cancels the others when one fails, and waits for every one to stop.",
    "Shielding protects an inner task from a cancel request, which suits work that must finish.",
]


def wrap(sentences: list[str], width: int) -> str:
    return textwrap.fill(" ".join(sentences), width, break_on_hyphens=False)


def ends_a_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?"))


@pytest.mark.parametrize("width", [40, 60, 72, 80, 100])
@pytest.mark.parametrize(
    "paragraphs",
    [
        [SENTENCES[0:2], SENTENCES[2:3], SENTENCES[3:6], SENTENCES[6:7]],
        [SENTENCES * 2],  # one paragraph longer than a passage may be
    ],
    ids=["short paragraphs", "one long paragraph"],
)
def test_no_passage_ends_in_the_middle_of_a_hard_wrapped_sentence(
    width: int, paragraphs: list[list[str]]
) -> None:
    md = "# Cancelling\n\n" + "\n\n".join(wrap(p, width) for p in paragraphs)
    passages = split_passages(md)
    assert passages
    for p in passages:
        assert len(p.text) <= MAX_PASSAGE_CHARS
        assert ends_a_sentence(p.text), p.text
        assert all(ends_a_sentence(line) for line in p.text.split("\n")), p.text
    said = [w for ps in paragraphs for s in ps for w in s.split()]
    assert [w for p in passages for w in p.text.split()] == said  # nothing lost or repeated


def test_a_paragraph_is_one_line_in_its_passage() -> None:
    (p,) = split_passages(wrap(SENTENCES[:3], 50))
    assert p.text == " ".join(SENTENCES[:3])


@pytest.mark.parametrize("abbreviation", ["e.g.", "i.e.", "etc.", "vs."])
def test_an_abbreviation_does_not_end_a_sentence(abbreviation: str) -> None:
    lead = " ".join(
        f"Sentence {n:02d} says one small thing about the garden and the pond." for n in range(10)
    )
    tail = f"See {abbreviation} the cases where a task is cancelled by its parent below."
    passages = split_passages(f"{lead} {tail}")
    assert len(passages) == 2  # the paragraph is over one passage long, so it is cut once
    assert not any(p.text.endswith(abbreviation) for p in passages)
    assert passages[1].text == tail


def test_a_closing_quote_bracket_or_mark_stays_with_its_sentence() -> None:
    curly = f"He said {chr(0x201C)}stop.{chr(0x201D)} Then he left."
    text = f'She said "stop." Then (quietly) left. It **ended.** Done? Yes. {curly}'
    assert _sentences(text) == [
        'She said "stop."',
        "Then (quietly) left.",
        "It **ended.**",
        "Done?",
        "Yes.",
        f"He said {chr(0x201C)}stop.{chr(0x201D)}",
        "Then he left.",
    ]


def test_a_line_that_starts_with_a_year_is_not_a_list_item() -> None:
    (p,) = split_passages(
        "The change shipped in\n2023. It was widely used by the time the next release arrived,\n"
        "and people kept asking about it in the forum."
    )
    assert "shipped in 2023. It was widely used" in p.text
    assert "\n" not in p.text


def test_a_wrapped_line_that_starts_with_a_year_stays_in_its_list_item() -> None:
    lines = [
        line
        for p in split_passages(
            "- The change shipped in\n2023. It was widely used by then.\n- Another item."
        )
        for line in p.text.split("\n")
    ]
    assert lines == ["- The change shipped in 2023. It was widely used by then.", "- Another item."]


def test_a_number_other_than_one_does_not_interrupt_a_paragraph_but_one_does() -> None:
    def lines(md: str) -> list[str]:
        return [line for p in split_passages(md) for line in p.text.split("\n")]

    assert lines("The reason is given in step\n2. above, and it still holds.") == [
        "The reason is given in step 2. above, and it still holds."
    ]
    assert lines("Steps follow:\n1. Install it.\n2. Run it.") == [
        "Steps follow:",
        "1. Install it.",
        "2. Run it.",
    ]


def test_a_fence_that_is_never_closed_runs_to_the_end_of_the_text() -> None:
    passages = split_passages("Some text first.\n\n```\nfirst_call()\n# second\nsecond_call()")
    assert [line for p in passages for line in p.text.split("\n")] == [
        "Some text first.",
        "first_call()",
        "# second",
        "second_call()",
    ]
    assert {p.heading for p in passages} == {None}  # "# second" is code here too


def test_inline_code_in_triple_backticks_is_not_a_fence() -> None:
    passages = split_passages("```not a fence``` is how it starts, and it reads on.\n\nMore text.")
    assert [line for p in passages for line in p.text.split("\n")] == [
        "```not a fence``` is how it starts, and it reads on.",
        "More text.",
    ]


def test_a_list_item_that_wraps_is_one_line_and_each_item_is_kept_whole() -> None:
    md = (
        "Steps follow:\n"
        "- The first step is long enough that the source wraps it onto a\n"
        "second line and then a third line, for good measure.\n"
        "- A short step.\n"
        "1. Install the package.\n"
        "2. Run the server and\nattach a client.\n"
    )
    lines = [line for p in split_passages(md) for line in p.text.split("\n")]
    assert lines == [
        "Steps follow:",
        "- The first step is long enough that the source wraps it onto a second line and then a "
        "third line, for good measure.",
        "- A short step.",
        "1. Install the package.",
        "2. Run the server and attach a client.",
    ]


def test_table_rows_stay_one_per_line_next_to_a_paragraph() -> None:
    md = "Prices are below.\n| Plan | Price |\n|---|---|\n| Team | 12 |\n| Solo | 0 |"
    lines = [line for p in split_passages(md) for line in p.text.split("\n")]
    assert lines == ["Prices are below.", "| Plan | Price |", "| Team | 12 |", "| Solo | 0 |"]


def test_a_code_block_is_one_unit_and_its_comments_are_not_headings() -> None:
    md = (
        "# Cancelling\n\nStart the work, then cancel it.\n\n"
        "```\ntask = asyncio.create_task(work())\n# stop it\ntask.cancel()\n```\n"
        "After the code the prose carries on and says what happened."
    )
    passages = split_passages(md)
    assert {p.heading for p in passages} == {"Cancelling"}  # "# stop it" is code
    lines = [line for p in passages for line in p.text.split("\n")]
    assert lines == [
        "Start the work, then cancel it.",
        "task = asyncio.create_task(work())",
        "# stop it",
        "task.cancel()",
        "After the code the prose carries on and says what happened.",
    ]
    assert "```" not in "".join(p.text for p in passages)


def test_a_long_code_block_is_cut_between_lines() -> None:
    code = [f"result_{n} = await asyncio.wait_for(work({n}), timeout=30)" for n in range(30)]
    passages = split_passages("```\n" + "\n".join(code) + "\n```")
    assert len(passages) > 1
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)
    assert [line for p in passages for line in p.text.split("\n")] == code


@pytest.mark.parametrize(
    "text",
    [
        ". " * 100_000,
        '."' * 100_000,
        "a. " * 70_000,
        ("x" * 1000 + ". ") * 150,
        " " * 200_000 + "x",
        "- " * 100_000,
        "1. " * 100_000,
        "|" * 200_000,
        "word\n" * 100_000,
        "```\n" * 50_000,
    ],
    ids=lambda t: f"{t[:6]!r}x{len(t)}",
)
def test_hostile_text_is_split_in_a_short_time(text: str) -> None:
    start = time.perf_counter()
    passages = split_passages(text)
    assert time.perf_counter() - start < 5
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)


# --- keeping the top hits apart (Phase 8) ---------------------------------------------------


def hit(url: str, heading: str | None, text: str, score: float = 1.0) -> Hit:
    return Hit(url, "A page", heading, text, score)


def test_no_more_than_two_hits_come_from_one_section() -> None:
    assert MAX_HITS_PER_SECTION == 2
    same = [hit("http://a.test/p", "Cancel", f"Passage {n}, about cancelling.") for n in range(5)]
    other = hit("http://a.test/p", "Timeouts", "A timeout cancels the task when time is up.")
    assert diverse([*same, other], 10) == [same[0], same[1], other]


def test_a_section_is_a_heading_on_one_page() -> None:
    texts = iter(f"Passage {n}, about cancelling." for n in range(20))
    hits = [
        hit(url, heading, next(texts))
        for url in ("http://a.test/1", "http://a.test/2")
        for heading in ("Intro", None)
        for _ in range(3)
    ]
    kept = diverse(hits, 20)
    assert Counter((h.url, h.heading) for h in kept) == {
        ("http://a.test/1", "Intro"): 2,
        ("http://a.test/1", None): 2,
        ("http://a.test/2", "Intro"): 2,
        ("http://a.test/2", None): 2,
    }


def test_the_hits_that_are_left_keep_their_order_and_stop_at_the_limit() -> None:
    hits = [hit(f"http://a.test/{n}", None, f"Passage {n}, about cancelling.") for n in range(8)]
    assert diverse(hits, 3) == hits[:3]
    assert diverse(hits, 20) == hits
    assert diverse([], 3) == []


PARAGRAPH = (
    "Do not swallow the error. If a task catches the cancel error and carries on, anyone who "
    "awaits the task will believe it finished normally, and a request to cancel the task has "
    "been quietly ignored. Raise the error again after the clean-up, so that the caller sees "
    "that the work was stopped."
)
OTHER_PARAGRAPH = (
    "A group of tasks cancels the others when one of them fails. Each remaining task receives "
    "the request, and the group waits until every one of them has stopped before it raises, so "
    "that nothing is left running behind the block that created the tasks in the first place."
)


def test_a_near_duplicate_is_dropped_and_the_better_hit_is_kept() -> None:
    a = hit("http://a.test/x", "One", PARAGRAPH, 3.0)
    b = hit("http://b.test/y", "Two", PARAGRAPH.replace("quietly", "silently"), 2.0)
    c = hit("http://c.test/z", "Three", OTHER_PARAGRAPH, 1.0)
    assert diverse([a, b, c], 10) == [a, c]
    assert diverse([b, a, c], 10) == [b, c]  # whichever ranks first stays


def test_a_passage_with_two_words_changed_is_a_near_copy_and_one_with_three_is_not() -> None:
    """The limit is 12 bits and inclusive. These variants sit at 12 and at 15 bits."""
    two = PARAGRAPH.replace("quietly", "silently").replace("swallow", "ignore")
    three = two.replace("finished", "stopped")
    a = hit("http://a.test/x", "One", PARAGRAPH, 3.0)
    assert diverse([a, hit("http://b.test/y", "Two", two, 2.0)], 10) == [a]
    assert len(diverse([a, hit("http://b.test/y", "Two", three, 2.0)], 10)) == 2


def test_a_passage_under_thirty_words_is_only_ever_an_exact_repeat() -> None:
    """A simhash of a short text is too noisy to trust. These two are 9 bits apart, close enough
    to merge a long passage, and say different things."""
    short = " ".join(PARAGRAPH.split()[:28])
    other = short.replace(" the ", " an ", 1)
    assert len(short.split()) < 30 and other != short
    a, b = hit("http://a.test/x", "One", short), hit("http://b.test/y", "Two", other)
    assert diverse([a, b], 10) == [a, b]


def test_two_different_passages_on_one_topic_are_both_kept() -> None:
    a = hit("http://a.test/x", "One", PARAGRAPH)
    b = hit("http://b.test/y", "Two", OTHER_PARAGRAPH)
    assert diverse([a, b], 10) == [a, b]


def test_a_short_repeat_in_other_case_and_spacing_is_dropped_by_its_words() -> None:
    a = hit("http://a.test/x", "One", "Refunds are paid within thirty days.")
    b = hit("http://b.test/y", "Two", "refunds  are paid\nwithin thirty DAYS.")
    c = hit("http://c.test/z", "Three", "Refunds are paid within forty days.")  # a different fact
    assert diverse([a, b, c], 10) == [a, c]


def test_the_top_five_span_more_than_one_section_when_they_can() -> None:
    strong = [  # six different things to say about cancelling a task, none a copy of another
        "To cancel a task, call cancel on it. The task does not stop at once: it is told at its "
        "next await, so a task that is busy keeps running until it yields to the loop.",
        "A task that is asked to cancel may clean up first. Put the awaited work in a try block "
        "with a finally clause, so that files and locks are released when the task is cancelled.",
        "Do not swallow the error. If a task catches it and carries on, whoever awaits the task "
        "will think it finished normally, and the request to cancel was quietly ignored.",
        "Afterwards you can ask whether a task was cancelled. The answer is true once the task has "
        "stopped because of such a request, and awaiting that task raises the error again.",
        "To cancel many tasks at once, keep them in a list and call cancel on each, then gather "
        "the list, so that one cancelled task does not hide the results of all the others.",
        "A request to cancel can carry a message. The text travels with the error, which helps "
        "when several parts of a program may cancel the same task for different reasons.",
    ]
    md = (
        "# Cancelling tasks\n\n## Task.cancel\n\n"
        + "\n\n".join(strong)
        + "\n\n## Timeouts\n\nA timeout will cancel the task it guards when the time is up, and "
        "the caller then sees a different error from the one raised inside the block."
        "\n\n## TaskGroup\n\nA group cancels its other tasks when one of them fails, then waits "
        "for each of them to stop before it raises an error of its own."
    )
    hits, _ = rank_passages("cancel a task", [("http://a.test/c", "Cancelling tasks", md)])
    # Without the limit the best five are one section's, six times over.
    assert {h.heading for h in hits[:5]} == {"Task.cancel"}
    top = diverse(hits, 5)
    sections = Counter((h.url, h.heading) for h in top)
    assert len(top) == 4 and len(sections) == 3
    assert max(sections.values()) <= MAX_HITS_PER_SECTION
    assert top[0].heading == "Task.cancel"  # the best hit is still first
