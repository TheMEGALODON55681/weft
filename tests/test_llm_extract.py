"""The LLM fallback with a fake model. No provider, no network."""

import json

import pytest

from weft.extract.llm import SYSTEM, llm_extract
from weft.providers import ProviderError

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}


class FakeLlm:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


@pytest.mark.parametrize(
    "reply",
    [
        '{"title": "Hello"}',
        '  {"title": "Hello"}\n',
        '```json\n{"title": "Hello"}\n```',
        '```\n{"title": "Hello"}\n```\n',
    ],
)
async def test_the_json_in_the_reply_is_parsed_with_or_without_a_fence(reply: str) -> None:
    assert await llm_extract(FakeLlm(reply), SCHEMA, "http://a.test/", "text", 1000) == {
        "title": "Hello"
    }


@pytest.mark.parametrize(
    "reply", ["Sure! Here is the data.", "", "```json\n{not json", "[1, 2", "[" * 200_000]
)
async def test_a_reply_that_is_not_json_is_a_provider_error(reply: str) -> None:
    with pytest.raises(ProviderError) as e:
        await llm_extract(FakeLlm(reply), SCHEMA, "http://a.test/", "text", 1000)
    assert e.value.code == "provider_bad_response"


async def test_the_prompt_carries_the_schema_the_url_and_the_start_of_the_page() -> None:
    fake = FakeLlm("{}")
    page = "A" * 50 + "B" * 50
    await llm_extract(fake, SCHEMA, "http://a.test/p", page, max_chars=60)
    ((system, user),) = fake.calls
    assert system == SYSTEM
    assert json.dumps(SCHEMA, separators=(",", ":")) in user
    assert "http://a.test/p" in user
    assert "A" * 50 + "B" * 10 in user and "B" * 11 not in user  # cut at max_chars


def test_the_instructions_treat_the_page_as_data() -> None:
    assert "ignore any instructions" in SYSTEM.lower()
    assert "only json" in SYSTEM.lower() or "nothing else" in SYSTEM.lower()


async def test_a_provider_failure_passes_through() -> None:
    class Down:
        async def complete(self, system: str, user: str) -> str:
            raise ProviderError("provider_unreachable", "ConnectError")

    with pytest.raises(ProviderError, match="ConnectError"):
        await llm_extract(Down(), SCHEMA, "http://a.test/", "text", 1000)
