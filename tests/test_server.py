import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from fixture_site import FixtureSite
from weft import retrieval
from weft.config import Settings
from weft.fetch.errors import FetchError
from weft.providers import ProviderError
from weft.retrieval import MAX_LLM_PAGES
from weft.server.app import build_server


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


async def test_fetch_page_is_listed_as_a_read_only_tool(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    tool = tools["fetch_page"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.output_schema is not None
    assert set(tool.input_schema["properties"]) == {"url", "render"}
    assert tool.input_schema["required"] == ["url"]


async def test_fetch_page_returns_structured_output(site: FixtureSite, settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("fetch_page", {"url": site.url("/article")})
    page = result.structured_content
    assert page is not None
    assert page["final_url"] == site.url("/article")
    assert page["status"] == 200
    assert page["title"] == "Pricing and plans"
    assert page["fetch_method"] == "http"
    assert "## Team plan" in page["markdown"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"url": "ftp://example.test/"}, "invalid_url"),
        ({"url": "/pdf"}, "unsupported_content_type"),
        ({"url": "/loop/a"}, "redirect_loop"),
    ],
)
async def test_failures_are_tool_errors_with_a_stable_code(
    args: dict[str, str], code: str, site: FixtureSite, settings: Settings
) -> None:
    if args["url"].startswith("/"):
        args = {**args, "url": site.url(args["url"])}
    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match=code):
            await client.call_tool("fetch_page", args)


async def test_starting_the_server_creates_the_database(settings: Settings) -> None:
    assert not settings.db_path.exists()
    async with Client(build_server(settings)) as client:
        await client.list_tools()
        assert settings.db_path.exists()


async def test_real_stdio_transport_round_trip(site: FixtureSite, tmp_path: Path) -> None:
    """Spawn the server the way an MCP client would and talk to it over pipes."""
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "weft"],
        env={
            "WEFT_DATA_DIR": str(tmp_path / "data"),
            "WEFT_LOG_LEVEL": "INFO",
            # The child gets a minimal environment, not the test's; it must reach the loopback site.
            "WEFT_ALLOW_LOOPBACK": "1",
            "WEFT_HOST_MIN_INTERVAL_S": "0",
        },
        keep_alive=False,
    )
    async with Client(transport) as client:
        names = {t.name for t in await client.list_tools()}
        result = await client.call_tool("fetch_page", {"url": site.url("/article")})
    assert "fetch_page" in names
    assert result.structured_content is not None
    assert result.structured_content["title"] == "Pricing and plans"
    assert json.dumps(result.structured_content)  # fully serialisable
    assert (tmp_path / "data" / "weft.db").exists()


# --- Phase 4 tools -------------------------------------------------------------------------


async def test_the_crawl_tools_are_listed(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert {"crawl_site", "get_job", "cancel_job", "list_jobs", "map_site"} <= set(tools)
    crawl = tools["crawl_site"].input_schema
    assert crawl["required"] == ["seed_urls"]
    assert {"goal", "max_pages", "max_depth", "include_patterns", "exclude_patterns"} <= set(
        crawl["properties"]
    )
    for name in ("get_job", "list_jobs", "map_site"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is True


async def test_crawl_site_reports_progress_and_returns_ranked_pages(
    site: FixtureSite, settings: Settings
) -> None:
    progress: list[float] = []

    async def on_progress(done: float, total: float | None, _msg: str | None) -> None:
        progress.append(done)

    async with Client(build_server(settings), progress_handler=on_progress) as client:
        result = await client.call_tool(
            "crawl_site",
            {
                "seed_urls": [site.url("/site/")],
                "goal": "pricing",
                "max_pages": 6,
                "wait_s": 30,
            },
        )
        job = result.structured_content
        assert job is not None
        assert job["job"]["status"] == "done"
        top = {p["url"].removeprefix(site.base) for p in job["pages"][:2]}
        assert top == {"/site/pricing", "/site/pricing/enterprise"}
        assert progress and progress == sorted(progress)

        again = await client.call_tool("get_job", {"job_id": job["job"]["id"], "limit": 1})
        assert again.structured_content is not None
        assert len(again.structured_content["pages"]) == 1
        listed = await client.call_tool("list_jobs", {})
        assert listed.structured_content is not None
        assert [j["id"] for j in listed.structured_content["jobs"]] == [job["job"]["id"]]
        # The crawl filled the page cache, so the full text costs no second request. Both use
        # the default render mode, which is part of the cache key.
        hits = site.hits("/site/pricing")
        page = await client.call_tool("fetch_page", {"url": site.url("/site/pricing")})
        assert page.structured_content is not None
        assert page.structured_content["cached"] is True
        assert site.hits("/site/pricing") == hits


async def test_unknown_jobs_and_bad_seeds_are_tool_errors(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="unknown_job"):
            await client.call_tool("get_job", {"job_id": "nope"})
        with pytest.raises(ToolError, match="unknown_job"):
            await client.call_tool("cancel_job", {"job_id": "nope"})
        with pytest.raises(ToolError, match="invalid_url"):
            await client.call_tool("crawl_site", {"seed_urls": ["ftp://x.test/"]})


async def test_a_restarted_server_resumes_its_job(site: FixtureSite, tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", host_min_interval_s=0.3)
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "crawl_site",
            {"seed_urls": [site.url("/site/")], "max_pages": 8, "render": "never", "wait_s": 1},
        )
        assert result.structured_content is not None
        job_id = result.structured_content["job"]["id"]
        assert result.structured_content["job"]["status"] == "running"
    # The server stopped mid-crawl. A new one picks the job up at startup.
    async with Client(build_server(Settings(data_dir=tmp_path / "data"))) as client:
        for _ in range(100):
            info = (await client.call_tool("get_job", {"job_id": job_id})).structured_content
            assert info is not None
            if info["job"]["status"] != "running":
                break
            await asyncio.sleep(0.1)
    assert info["job"]["status"] == "done"
    assert info["job"]["stats"]["pages_fetched"] == 8


async def test_map_site_over_mcp(site: FixtureSite, settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("map_site", {"url": site.url("/site/")})
    m = result.structured_content
    assert m is not None
    assert any(u["url"].endswith("/site/blog/1") and u["source"] == "sitemap" for u in m["urls"])


# --- Phase 5 tools: search_in_site and extract ---------------------------------------------


class FakeEmbedder:
    """Each passage's vector gets further from the query's the earlier it comes, so meaning
    prefers the last keyword hit. Records every call."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise ProviderError("provider_unreachable", "ConnectError")
        n = len(texts) - 1
        angles = [0.1 * (n - 1 - i) for i in range(n)]
        return [[1.0, 0.0], *([math.cos(a), math.sin(a)] for a in angles)]


class FakeLlm:
    def __init__(self, reply: str = "{}") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.reply


def rerank_settings(data_dir: Path) -> Settings:
    """The flag on, with an endpoint that nothing can reach: the fakes are injected, so a call
    that went anywhere else would fail the test."""
    return Settings(
        data_dir=data_dir,
        embedding_rerank=True,
        embedding_base_url="http://models.invalid/v1",
        embedding_model="m",
    )


def llm_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        llm_extraction=True,
        llm_base_url="http://models.invalid/v1",
        llm_model="m",
    )


SEARCH = {"query": "pricing team plan seat", "max_pages": 10, "wait_s": 30}


async def test_the_retrieval_tools_are_listed(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    search, extract = tools["search_in_site"], tools["extract"]
    assert search.input_schema["required"] == ["url", "query"]
    assert {"max_pages", "limit", "wait_s"} <= set(search.input_schema["properties"])
    assert extract.input_schema["required"] == ["url_or_job", "schema"]
    assert extract.annotations is not None and extract.annotations.read_only_hint is True
    assert "x-css" in (extract.description or "")  # the agent has to learn the rule syntax here


async def test_search_in_site_returns_ranked_passages_with_source_and_snippet(
    site: FixtureSite, settings: Settings
) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
    r = result.structured_content
    assert r is not None
    top = r["hits"][0]
    assert top["url"] == site.url("/site/pricing")  # the page to cite
    assert "twelve dollars" in top["snippet"] and "twelve dollars" in top["passage"]
    assert top["title"] == "Pricing"
    scores = [h["score"] for h in r["hits"]]
    assert scores == sorted(scores, reverse=True)
    assert r["complete"] is True and r["job"]["status"] == "done"
    assert r["job"]["params"]["goal"] == SEARCH["query"]  # the crawl is steered by the query
    assert r["job"]["params"]["max_pages"] == SEARCH["max_pages"]
    assert r["pages_searched"] >= 3 and r["passages_searched"] >= r["pages_searched"]
    assert r["reranked"] is False
    assert len(r["hits"]) <= 10


async def test_search_in_site_says_so_when_the_crawl_is_not_finished(
    site: FixtureSite, tmp_path: Path
) -> None:
    slow = Settings(data_dir=tmp_path / "data", host_min_interval_s=0.4)
    async with Client(build_server(slow)) as client:
        result = await client.call_tool(
            "search_in_site",
            {"url": site.url("/site/"), "query": "pricing", "max_pages": 30, "wait_s": 1},
        )
        r = result.structured_content
        assert r is not None
        assert r["complete"] is False and r["job"]["status"] == "running"
        assert r["pages_searched"] >= 1  # what was crawled by then is searched
        await client.call_tool("cancel_job", {"job_id": r["job"]["id"]})  # nothing left running


async def test_a_query_with_no_match_returns_no_hits(site: FixtureSite, settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "search_in_site", {"url": site.url("/site/"), "query": "zebra", "max_pages": 4}
        )
    assert result.structured_content is not None
    assert result.structured_content["hits"] == []
    assert result.structured_content["pages_searched"] >= 1


async def test_a_bad_query_or_url_is_refused_before_any_request(
    site: FixtureSite, settings: Settings
) -> None:
    before = site.total_hits
    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="invalid_query"):
            await client.call_tool("search_in_site", {"url": site.url("/site/"), "query": "the of"})
        with pytest.raises(ToolError, match="invalid_url"):
            await client.call_tool("search_in_site", {"url": "ftp://a.test/", "query": "pricing"})
        listed = await client.call_tool("list_jobs", {})  # and no job was started for either
        assert listed.structured_content == {"jobs": []}
    assert site.total_hits == before


async def test_the_embedding_provider_is_never_called_unless_the_flag_is_on(
    site: FixtureSite, settings: Settings
) -> None:
    fake = FakeEmbedder()
    async with Client(build_server(settings, embedder=fake)) as client:  # flag off, fake given
        result = await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
    assert fake.calls == []
    assert result.structured_content is not None and result.structured_content["reranked"] is False


async def test_rerank_reorders_the_keyword_hits_when_the_flag_is_on(
    site: FixtureSite, settings: Settings, tmp_path: Path
) -> None:
    async with Client(build_server(settings)) as client:
        plain = (
            await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
        ).structured_content
    assert plain is not None and len(plain["hits"]) >= 3  # three are needed for a reorder to show
    fake = FakeEmbedder()
    async with Client(build_server(rerank_settings(tmp_path / "b"), embedder=fake)) as client:
        reranked = (
            await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
        ).structured_content
    assert reranked is not None and reranked["reranked"] is True
    assert len(fake.calls) == 1 and len(fake.calls[0]) == 1 + len(reranked["hits"])
    assert fake.calls[0][0] == SEARCH["query"]  # the query goes first
    before = [h["passage"] for h in plain["hits"]]
    after = [h["passage"] for h in reranked["hits"]]
    assert after[0] == before[0] and sorted(after) == sorted(before) and after != before


async def test_a_failing_embedding_provider_leaves_the_keyword_order(
    site: FixtureSite, settings: Settings, tmp_path: Path
) -> None:
    async with Client(build_server(settings)) as client:
        plain = (
            await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
        ).structured_content
    fake = FakeEmbedder(fail=True)
    async with Client(build_server(rerank_settings(tmp_path / "b"), embedder=fake)) as client:
        down = (
            await client.call_tool("search_in_site", {"url": site.url("/site/"), **SEARCH})
        ).structured_content
    assert plain is not None and down is not None
    assert len(fake.calls) == 1 and down["reranked"] is False
    assert [h["passage"] for h in down["hits"]] == [h["passage"] for h in plain["hits"]]


ROLE = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "x-css": "h1"},
        "salary": {"type": "integer", "x-css": ".salary"},
    },
    "required": ["title", "salary"],
}
RULELESS = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


async def test_extract_reads_a_url_by_css_and_xpath_rules(
    site: FixtureSite, settings: Settings
) -> None:
    records = {
        "type": "array",
        "x-css": "div.role",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "x-xpath": ".//h2"},
                "pay": {"type": "integer", "x-css": ".pay"},
                "url": {"type": "string", "x-css": "a", "x-attr": "href"},
            },
            "required": ["title", "pay", "url"],
        },
    }
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "extract", {"url_or_job": site.url("/site/jobs"), "schema": records}
        )
    r = result.structured_content
    assert r is not None and r["source"] == "url" and r["job_id"] is None
    (item,) = r["items"]
    assert item["url"] == site.url("/site/jobs") and item["valid"] and item["method"] == "rules"
    assert item["data"] == [
        {"title": "Data Analyst", "pay": 80500, "url": site.url("/site/jobs/1")},
        {"title": "Designer", "pay": 95000, "url": site.url("/site/jobs/2")},
    ]
    assert (r["pages_checked"], r["pages_matched"], r["pages_unreadable"]) == (1, 1, 0)


async def test_extract_over_a_job_returns_the_pages_that_match_and_fetches_nothing_more(
    site: FixtureSite, settings: Settings
) -> None:
    async with Client(build_server(settings)) as client:
        crawl = await client.call_tool(
            "crawl_site",
            {
                "seed_urls": [site.url("/site/jobs")],
                "max_pages": 10,
                "wait_s": 30,
                "render": "never",  # the cache is keyed by render mode, so the read-back must
            },  # use the job's
        )
        assert crawl.structured_content is not None
        job_id = crawl.structured_content["job"]["id"]
        before = site.total_hits
        result = await client.call_tool("extract", {"url_or_job": job_id, "schema": ROLE})
    r = result.structured_content
    assert r is not None and r["source"] == "job" and r["job_id"] == job_id
    assert {i["url"].removeprefix(site.base): i["data"] for i in r["items"]} == {
        "/site/jobs/1": {"title": "Data Analyst", "salary": 80500},
        "/site/jobs/2": {"title": "Designer", "salary": 95000},
    }
    assert all(i["valid"] for i in r["items"])
    assert (r["pages_checked"], r["pages_matched"]) == (3, 2)  # the list page has no salary
    assert r["pages_total"] == 3 and r["job_status"] == "done"
    assert site.total_hits == before  # the HTML came from the cache


async def test_a_url_that_does_not_match_comes_back_invalid_with_the_reasons(
    site: FixtureSite, settings: Settings
) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "extract", {"url_or_job": site.url("/site/jobs"), "schema": ROLE}
        )
    r = result.structured_content
    assert r is not None
    (item,) = r["items"]
    assert item["valid"] is False and item["data"] == {"title": "Careers"}
    assert item["errors"] == ["(root): 'salary' is a required property"]
    assert r["pages_matched"] == 0


async def test_a_bad_schema_is_refused_before_any_request(
    site: FixtureSite, settings: Settings
) -> None:
    bad_schemas = [
        {"type": "object", "properties": {"a": {"x-css": "div["}}},
        {"type": "strng"},
        {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/x"}}},
        {"type": "object", "description": "x" * 60_000},
    ]
    before = site.total_hits
    async with Client(build_server(settings)) as client:
        for schema in bad_schemas:
            with pytest.raises(ToolError, match="invalid_schema"):
                await client.call_tool(
                    "extract", {"url_or_job": site.url("/site/jobs"), "schema": schema}
                )
    assert site.total_hits == before


async def test_extract_reports_unknown_jobs_and_unusable_pages(
    site: FixtureSite, settings: Settings
) -> None:
    async with Client(build_server(settings)) as client:
        with pytest.raises(ToolError, match="unknown_job"):
            await client.call_tool("extract", {"url_or_job": "nope", "schema": ROLE})
        with pytest.raises(ToolError, match="unsupported_content_type"):
            await client.call_tool("extract", {"url_or_job": site.url("/pdf"), "schema": ROLE})


async def test_the_llm_is_asked_only_when_the_flag_is_on_and_the_rules_fall_short(
    site: FixtureSite, settings: Settings, tmp_path: Path
) -> None:
    reply = json.dumps({"title": "Careers", "salary": 1})
    args = {"url_or_job": site.url("/site/jobs"), "schema": ROLE}
    off = FakeLlm(reply)
    async with Client(build_server(settings, llm=off)) as client:  # flag off, fake given
        r = (await client.call_tool("extract", args)).structured_content
    assert off.calls == [] and r is not None and r["items"][0]["valid"] is False

    on = FakeLlm(reply)
    async with Client(build_server(llm_settings(tmp_path / "b"), llm=on)) as client:
        r = (await client.call_tool("extract", args)).structured_content
        ok = (
            await client.call_tool(  # a page the rules satisfy does not need the model
                "extract", {"url_or_job": site.url("/site/jobs/1"), "schema": ROLE}
            )
        ).structured_content
    assert r is not None and ok is not None
    assert len(on.calls) == 1
    (item,) = r["items"]
    assert item["method"] == "llm" and item["valid"] and item["data"]["salary"] == 1
    assert "Careers" in on.calls[0] and site.url("/site/jobs") in on.calls[0]
    assert ok["items"][0]["method"] == "rules" and len(on.calls) == 1


async def test_an_llm_answer_that_breaks_the_schema_does_not_replace_what_the_rules_found(
    site: FixtureSite, tmp_path: Path
) -> None:
    for reply, note in (
        ('{"title": "X"}', "did not satisfy"),
        ("sorry, no", "provider_bad_response"),
    ):
        llm = FakeLlm(reply)
        async with Client(build_server(llm_settings(tmp_path / note), llm=llm)) as client:
            r = (
                await client.call_tool(
                    "extract", {"url_or_job": site.url("/site/jobs"), "schema": ROLE}
                )
            ).structured_content
        assert r is not None
        (item,) = r["items"]
        assert item["method"] == "rules" and item["valid"] is False
        assert item["data"] == {"title": "Careers"}  # what the rules found, not the model's
        assert any(note in e for e in item["errors"]), item["errors"]
        assert "(root): 'salary' is a required property" in item["errors"]  # reasons stay


async def test_a_schema_with_no_rules_is_for_the_model_alone(
    site: FixtureSite, settings: Settings, tmp_path: Path
) -> None:
    args = {"url_or_job": site.url("/site/jobs"), "schema": RULELESS}
    async with Client(build_server(settings)) as client:
        r = (await client.call_tool("extract", args)).structured_content
    assert r is not None and r["items"][0]["valid"] is False
    assert "x-css" in r["items"][0]["errors"][0]  # says what is missing and how to fix it
    llm = FakeLlm('{"title": "Careers"}')
    async with Client(build_server(llm_settings(tmp_path / "b"), llm=llm)) as client:
        r = (await client.call_tool("extract", args)).structured_content
    assert r is not None and r["items"][0]["method"] == "llm" and r["items"][0]["valid"]


async def test_one_extract_call_asks_the_model_about_a_bounded_number_of_pages(
    site: FixtureSite, tmp_path: Path
) -> None:
    llm = FakeLlm('{"title": "found"}')
    async with Client(build_server(llm_settings(tmp_path), llm=llm)) as client:
        crawl = await client.call_tool(
            "crawl_site",
            {"seed_urls": [site.url("/site/")], "max_pages": 30, "max_depth": 3, "wait_s": 60},
        )
        assert crawl.structured_content is not None
        job_id = crawl.structured_content["job"]["id"]
        r = (
            await client.call_tool("extract", {"url_or_job": job_id, "schema": RULELESS})
        ).structured_content
    assert r is not None
    assert r["pages_checked"] > MAX_LLM_PAGES
    assert len(llm.calls) == MAX_LLM_PAGES == r["pages_matched"]


async def test_limit_caps_the_hits(site: FixtureSite, settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool(
            "search_in_site", {"url": site.url("/site/"), **SEARCH, "limit": 1}
        )
    assert result.structured_content is not None and len(result.structured_content["hits"]) == 1


async def test_a_page_that_satisfies_the_schema_by_finding_nothing_is_not_a_match(
    site: FixtureSite, settings: Settings
) -> None:
    anything = {"type": "object", "properties": {"salary": {"type": "integer", "x-css": ".salary"}}}
    async with Client(build_server(settings)) as client:
        crawl = await client.call_tool(
            "crawl_site", {"seed_urls": [site.url("/site/jobs")], "max_pages": 10, "wait_s": 30}
        )
        assert crawl.structured_content is not None
        job_id = crawl.structured_content["job"]["id"]
        every = (
            await client.call_tool("extract", {"url_or_job": job_id, "schema": anything})
        ).structured_content
        one = (
            await client.call_tool(
                "extract", {"url_or_job": job_id, "schema": anything, "limit": 1}
            )
        ).structured_content
        empty = (
            await client.call_tool(
                "extract", {"url_or_job": site.url("/site/about"), "schema": anything}
            )
        ).structured_content
    assert every is not None and one is not None and empty is not None
    assert sorted(i["data"]["salary"] for i in every["items"]) == [80500, 95000]
    assert every["pages_checked"] == 3  # the list page is valid with {} and is not returned
    assert one["pages_matched"] == 1 and one["pages_checked"] < one["pages_total"]  # it stops early
    assert empty["items"][0]["valid"] is True and empty["pages_matched"] == 0


async def test_pages_that_cannot_be_read_again_are_counted_and_skipped(
    site: FixtureSite, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = retrieval.get_document

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        if args[2].endswith("/site/jobs/2"):  # (fetcher, settings, url, ...)
            raise FetchError("network_error", "gone")
        return await real(*args, **kwargs)

    monkeypatch.setattr(retrieval, "get_document", flaky)  # the crawl has its own reference
    async with Client(build_server(settings)) as client:
        found = (
            await client.call_tool(
                "search_in_site",
                {"url": site.url("/site/jobs"), "query": "designer data analyst", "max_pages": 10},
            )
        ).structured_content
        assert found is not None
        pulled = (
            await client.call_tool("extract", {"url_or_job": found["job"]["id"], "schema": ROLE})
        ).structured_content
    assert (found["pages_searched"], found["pages_unreadable"]) == (2, 1)
    assert all(not h["url"].endswith("/site/jobs/2") for h in found["hits"])
    assert pulled is not None
    assert [i["url"].removeprefix(site.base) for i in pulled["items"]] == ["/site/jobs/1"]
    assert (pulled["pages_checked"], pulled["pages_unreadable"]) == (3, 1)


async def test_an_extraction_that_would_return_too_much_text_is_refused(
    site: FixtureSite, tmp_path: Path
) -> None:
    small = Settings(data_dir=tmp_path / "data", max_content_chars=50)
    every_paragraph = {"type": "array", "x-css": "p", "items": {"type": "string"}}
    async with Client(build_server(small)) as client:
        r = (
            await client.call_tool(
                "extract", {"url_or_job": site.url("/site/jobs"), "schema": every_paragraph}
            )
        ).structured_content
    assert r is not None
    (item,) = r["items"]
    assert item["valid"] is False and item["data"] is None and r["pages_matched"] == 0
    assert "more than 50 characters" in item["errors"][0]


async def test_a_long_passage_comes_back_with_a_shorter_snippet(
    site: FixtureSite, settings: Settings
) -> None:
    async with Client(build_server(settings)) as client:
        found = (
            await client.call_tool(
                "search_in_site",
                {"url": site.url("/site/edge"), "query": "observability virtualization"},
            )
        ).structured_content
    assert found is not None
    top = found["hits"][0]
    assert len(top["passage"]) > 240 and len(top["snippet"]) < len(top["passage"])
    assert "observability" in top["snippet"]


async def test_a_query_with_no_match_does_not_call_the_embedding_provider(
    site: FixtureSite, tmp_path: Path
) -> None:
    fake = FakeEmbedder()
    async with Client(build_server(rerank_settings(tmp_path / "b"), embedder=fake)) as client:
        found = (
            await client.call_tool(
                "search_in_site", {"url": site.url("/site/"), "query": "zebra", "max_pages": 4}
            )
        ).structured_content
    assert found is not None and found["hits"] == []
    assert fake.calls == [] and found["reranked"] is False


async def test_the_scheme_may_be_upper_case_and_the_target_padded(
    site: FixtureSite, settings: Settings
) -> None:
    anything = {"type": "object", "properties": {"title": {"type": "string", "x-css": "h1"}}}
    async with Client(build_server(settings)) as client:
        for target in ("HTTP" + site.url("/site/jobs")[4:], f"  {site.url('/site/jobs')}\n"):
            r = (
                await client.call_tool("extract", {"url_or_job": target, "schema": anything})
            ).structured_content
            assert r is not None and r["source"] == "url"
            assert r["items"][0]["data"] == {"title": "Careers"}
