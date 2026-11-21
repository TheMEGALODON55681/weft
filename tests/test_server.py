import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from fixture_site import FixtureSite
from weft.config import Settings
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
