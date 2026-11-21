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
