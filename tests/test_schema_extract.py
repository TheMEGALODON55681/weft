"""Rule-based extraction: CSS and XPath rules written into a JSON Schema, then validated against it.
Pure functions over HTML strings, no network."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from weft.extract.schema import DataTooLarge, Extractor, InvalidSchema

BASE = "http://a.test/careers/"
PAGE = """<html><body>
<h1 class="title"> Senior   Engineer </h1>
<span class="salary">$120,000 per year</span>
<span class="rate">$95,000.50</span>
<a class="apply" href="/jobs/42">Apply</a>
<ul class="perks"><li>Remote</li><li>Equity</li></ul>
<div class="job"><h2>Data Analyst</h2><b class="pay">$80,500</b><a href="/jobs/1">Details</a></div>
<div class="job"><h2>Designer</h2><b class="pay">$95,000</b><a href="https://b.test/2">Details</a></div>
<div class="job"><h2>Intern</h2></div>
<div class="job"></div>
<table>
<tr class="row"><td>Alpha</td><td>3</td></tr>
<tr class="row"><td>Beta</td><td>5</td></tr>
</table>
<p class="free">Free shipping</p>
</body></html>"""


def run(schema: dict[str, Any], html: str = PAGE) -> Any:
    return Extractor(schema).run(html, BASE)


def obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props}


# --- scalars --------------------------------------------------------------------------------


def test_text_is_found_by_css_and_its_whitespace_collapsed() -> None:
    data = run(obj(title={"type": "string", "x-css": "h1.title"}))
    assert data == {"title": "Senior Engineer"}


def test_an_attribute_is_taken_and_a_link_is_made_absolute() -> None:
    data = run(obj(apply={"type": "string", "x-css": "a.apply", "x-attr": "href"}))
    assert data == {"apply": "http://a.test/jobs/42"}


def test_xpath_works_where_css_does_too() -> None:
    data = run(
        obj(
            title={"type": "string", "x-xpath": "//h1/text()"},
            apply={"type": "string", "x-xpath": "//a[@class='apply']/@href"},
            n={"type": "integer", "x-xpath": "count(//ul[@class='perks']/li)"},
        )
    )
    # An XPath that ends in an attribute gives it as written. Only x-attr makes a link absolute.
    assert data == {"title": "Senior Engineer", "apply": "/jobs/42", "n": 2}


def test_numbers_are_read_out_of_the_text_around_them() -> None:
    data = run(
        obj(
            salary={"type": "integer", "x-css": ".salary"},
            rate={"type": "number", "x-css": ".rate"},
        )
    )
    assert data == {"salary": 120000, "rate": 95000.5}
    assert isinstance(data["salary"], int)


def test_a_fraction_where_an_integer_is_wanted_is_kept_so_validation_can_say_so() -> None:
    ex = Extractor(obj(rate={"type": "integer", "x-css": ".rate"}))
    data = ex.run(PAGE, BASE)
    assert data == {"rate": 95000.5}
    assert any("rate" in e and "integer" in e for e in ex.errors(data))


def test_a_boolean_is_whether_the_selector_matches() -> None:
    data = run(
        obj(
            free={"type": "boolean", "x-css": "p.free"},
            gift={"type": "boolean", "x-css": "p.gift"},
        )
    )
    assert data == {"free": True, "gift": False}


def test_a_value_that_is_not_there_is_left_out() -> None:
    data = run(obj(title={"type": "string", "x-css": "h1.title"}, gone={"x-css": "h9"}))
    assert data == {"title": "Senior Engineer"}


def test_a_property_with_no_rule_is_left_out() -> None:
    assert run(obj(title={"type": "string"})) == {}


# --- arrays and records ---------------------------------------------------------------------


def test_an_array_of_scalars_takes_every_match() -> None:
    schema = {"type": "array", "x-css": ".perks li", "items": {"type": "string"}}
    assert run(obj(perks=schema)) == {"perks": ["Remote", "Equity"]}


def test_an_array_of_objects_makes_one_record_per_container() -> None:
    jobs = {
        "type": "array",
        "x-css": "div.job",
        "items": obj(
            title={"type": "string", "x-css": "h2"},
            pay={"type": "integer", "x-css": ".pay"},
            url={"type": "string", "x-css": "a", "x-attr": "href"},
        ),
    }
    assert run(jobs) == [
        {"title": "Data Analyst", "pay": 80500, "url": "http://a.test/jobs/1"},
        {"title": "Designer", "pay": 95000, "url": "https://b.test/2"},
        {"title": "Intern"},  # a record with only some fields is kept; one with none is dropped
    ]


def test_css_containers_with_xpath_fields_and_the_other_way_round() -> None:
    """Records are table rows, the shape a fragment parser would break, with the two kinds of
    rule mixed in one schema."""
    rows = {
        "type": "array",
        "x-css": "tr.row",
        "items": obj(
            name={"type": "string", "x-xpath": ".//td[1]"},
            count={"type": "integer", "x-xpath": "./td[2]"},
        ),
    }
    assert run(rows) == [{"name": "Alpha", "count": 3}, {"name": "Beta", "count": 5}]
    by_xpath = {
        "type": "array",
        "x-xpath": "//tr[@class='row']",
        "items": obj(name={"type": "string", "x-css": "td:first-child"}),
    }
    assert run(by_xpath) == [{"name": "Alpha"}, {"name": "Beta"}]


def test_an_object_rule_scopes_the_rules_inside_it() -> None:
    schema = obj(
        first=dict(
            obj(title={"type": "string", "x-css": "h2"}),
            **{"x-css": "div.job"},
        )
    )
    assert run(schema) == {"first": {"title": "Data Analyst"}}


def test_an_array_without_a_rule_is_left_out_and_one_that_matches_nothing_is_empty() -> None:
    assert run(obj(perks={"type": "array", "items": {"type": "string"}})) == {}
    assert run({"type": "array", "x-css": "div.nothing", "items": {"type": "string"}}) == []


# --- validation -----------------------------------------------------------------------------


def test_a_missing_required_value_is_reported_by_name() -> None:
    schema = {**obj(title={"type": "string", "x-css": "h9"}), "required": ["title"]}
    ex = Extractor(schema)
    data = ex.run(PAGE, BASE)
    assert ex.errors(data) == ["(root): 'title' is a required property"]
    assert Extractor(obj(a={"type": "string", "x-css": "h1"})).errors({"a": "x"}) == []


def test_errors_say_where_and_are_few_and_short() -> None:
    schema = {"type": "array", "items": {"type": "string", "maxLength": 3}}
    errors = Extractor(schema).errors(["x" * 5000] + ["abcdef"] * 30)  # the long one sorts first
    assert errors[0].startswith("0: 'xxx")
    assert len(errors) == 10
    assert all(len(e) <= 300 for e in errors)


def test_nothing_found_is_none() -> None:
    assert run(obj(title={"x-css": "h1"}), "") is None
    assert run(obj(title={"x-css": "h1"}), "   ") is None
    assert run(obj(title={"x-css": "h1"}), "<<<>>> not html at all") in (None, {})


# --- the schema itself ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "words"),
    [
        (obj(title={"type": "string", "x-css": "div["}), ["properties/title", "CSS"]),
        (obj(title={"type": "string", "x-xpath": "//["}), ["properties/title", "XPath"]),
        (obj(t={"x-css": "h1", "x-xpath": "//h1"}), ["properties/t", "both"]),
        (obj(t={"x-css": 5}), ["properties/t", "string"]),
        (obj(t={"type": "string", "x-attr": "href"}), ["properties/t", "x-attr"]),
        ({"type": "strng"}, ["not a valid JSON Schema"]),
        (obj(t={"x-css": "h" * 501}), ["properties/t", "long"]),
    ],
)
def test_a_bad_schema_is_refused_with_the_place_and_the_problem(
    schema: dict[str, Any], words: list[str]
) -> None:
    with pytest.raises(InvalidSchema) as e:
        Extractor(schema)
    for w in words:
        assert w in str(e.value)


def test_a_reference_outside_the_schema_is_refused() -> None:
    """jsonschema would not fetch it, but nothing should depend on that."""
    for ref in ("http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "other.json"):
        with pytest.raises(InvalidSchema, match=r"\$ref"):
            Extractor(obj(a={"$ref": ref}))


def test_a_reference_inside_the_schema_works() -> None:
    schema = {
        **obj(title={"$ref": "#/$defs/text", "x-css": "h1"}),
        "$defs": {"text": {"type": "string", "maxLength": 5}},
    }
    ex = Extractor(schema)
    assert ex.errors(ex.run(PAGE, BASE)) == ["title: 'Senior Engineer' is too long"]


def test_a_schema_that_nests_too_deep_or_is_too_big_is_refused() -> None:
    deep: dict[str, Any] = {"type": "string", "x-css": "h1"}
    for _ in range(12):
        deep = obj(a=deep)
    with pytest.raises(InvalidSchema, match="deep"):
        Extractor(deep)

    # The limits are the published ones: 300 nodes, counting the schema itself.
    def props(n: int) -> dict[str, Any]:
        return obj(**{f"p{i}": {"type": "string", "x-css": "h1"} for i in range(n)})

    Extractor(props(299))
    with pytest.raises(InvalidSchema, match="large"):
        Extractor(props(300))


def test_a_schema_is_refused_only_past_its_size_limit() -> None:
    def sized(n: int) -> dict[str, Any]:
        return {"type": "object", "description": "x" * n}

    spare = 50_000 - len(json.dumps(sized(0), separators=(",", ":")))
    Extractor(sized(spare))  # exactly at the limit
    with pytest.raises(InvalidSchema, match="characters"):
        Extractor(sized(spare + 1))


def test_a_selector_that_matches_the_same_text_at_every_level_is_stopped() -> None:
    """Each nested div's text holds all the text below it, so the answer grows with the square
    of the page. The budget counts characters as they are collected."""
    page = "<div>" * 50 + "x" * 1000 + "</div>" * 50
    ex = Extractor({"type": "array", "x-css": "div", "items": {"type": "string"}})
    assert len(ex.run(page, BASE)) == 50  # 50 000 characters, inside the default budget
    assert len(ex.run(page, BASE, max_chars=50_000)) == 50  # exactly the budget is allowed
    with pytest.raises(DataTooLarge, match="49999 characters"):
        ex.run(page, BASE, max_chars=49_999)
    flags = Extractor({"type": "array", "x-css": "div", "items": {"type": "boolean"}})
    with pytest.raises(DataTooLarge):
        flags.run(page, BASE, max_chars=10)  # a boolean costs a character too


def test_the_longest_allowed_selector_is_accepted() -> None:
    Extractor(obj(t={"x-css": "h" * 500}))


def test_json_nested_past_the_limit_is_refused_before_the_validator_walks_it() -> None:
    def nested(levels: int) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string"}
        for _ in range(levels):
            schema = {"not": schema}
        return schema

    Extractor(nested(30))
    with pytest.raises(InvalidSchema, match="deep"):
        Extractor(nested(100))


def test_a_negative_number_keeps_its_sign() -> None:
    page = '<span class="n">-12.5 degrees</span><span class="m">balance: -3</span>'
    schema = obj(n={"type": "number", "x-css": ".n"}, m={"type": "integer", "x-css": ".m"})
    assert run(schema, page) == {"n": -12.5, "m": -3}


def test_a_number_too_big_for_a_float_is_not_a_number() -> None:
    page = f'<span class="n">{"9" * 400}</span>'
    assert run(obj(n={"type": "integer", "x-css": ".n"}), page) == {}


def test_a_src_is_made_absolute_like_an_href() -> None:
    page = '<img class="logo" src="/img/a.png">'
    schema = obj(logo={"type": "string", "x-css": ".logo", "x-attr": "src"})
    assert run(schema, page) == {"logo": "http://a.test/img/a.png"}


def test_an_xpath_that_fails_as_it_runs_finds_nothing_and_opens_nothing() -> None:
    """XPath 1.0 has no function that reads a file or a URL. An unknown one fails when it runs."""
    schema = obj(a={"type": "string", "x-xpath": "document('file:///etc/passwd')"})
    assert run(schema) == {}


def test_errors_come_in_path_order_whatever_order_the_schema_lists_its_properties() -> None:
    ex = Extractor(obj(b={"type": "integer"}, a={"type": "integer"}))
    assert ex.errors({"a": "x", "b": "y"}) == [
        "a: 'x' is not of type 'integer'",
        "b: 'y' is not of type 'integer'",
    ]


def test_one_extractor_can_be_used_from_many_threads_at_once() -> None:
    ex = Extractor(
        obj(
            title={"type": "string", "x-css": "h1"},
            jobs={"type": "array", "x-css": "div.job", "items": obj(t={"x-css": "h2"})},
        )
    )
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: ex.run(PAGE, BASE), range(400)))
    assert results[0]["title"] == "Senior Engineer"
    assert all(r == results[0] for r in results)


def test_has_rules_says_whether_there_is_anything_to_run() -> None:
    assert Extractor(obj(a={"x-css": "h1"})).has_rules
    assert Extractor(obj(a={"type": "array", "items": obj(b={"x-xpath": "//b"})})).has_rules
    assert not Extractor(obj(a={"type": "string"})).has_rules


def test_the_prompt_schema_has_no_rules_in_it() -> None:
    schema = {**obj(a={"type": "string", "x-css": "h1", "description": "the title"})}
    clean = Extractor(schema).prompt_schema()
    assert "x-css" not in json.dumps(clean)
    assert clean["properties"]["a"] == {"type": "string", "description": "the title"}
    assert "x-css" in json.dumps(schema)  # the original is untouched
