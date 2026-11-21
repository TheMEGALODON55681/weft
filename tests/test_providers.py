"""The HTTP provider, driven through an in-memory transport: no socket is ever opened."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from weft.config import Settings
from weft.providers import (
    ChatClient,
    EmbeddingsClient,
    ProviderError,
    chat_from,
    embeddings_from,
)

KEY = "sk-secret-value"
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def embeddings(handler: Handler, key: str | None = KEY) -> EmbeddingsClient:
    return EmbeddingsClient("https://models.test/v1/", "embed-1", key, 5, client(handler))


def chat(handler: Handler, key: str | None = KEY) -> ChatClient:
    return ChatClient("https://models.test/v1", "chat-1", key, 5, client(handler))


async def test_embeddings_are_requested_in_one_call_and_returned_in_input_order() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        data = [  # out of order, as a server may send them
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
        return httpx.Response(200, json={"data": data})

    assert await embeddings(handler).embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    (req,) = seen
    assert str(req.url) == "https://models.test/v1/embeddings"  # the slash after v1/ is not doubled
    assert json.loads(req.content) == {"model": "embed-1", "input": ["a", "b"]}
    assert req.headers["Authorization"] == f"Bearer {KEY}"


async def test_chat_sends_a_system_and_a_user_message_and_returns_the_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}]})

    assert await chat(handler).complete("be brief", "the page") == '{"a": 1}'
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://models.test/v1/chat/completions"
    assert body["model"] == "chat-1"
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "the page"},
    ]
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}


async def test_no_key_means_no_authorization_header() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    await embeddings(handler, key=None).embed(["a"])
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("make_response", "code", "words"),
    [
        (lambda r: httpx.Response(500, text=f"upstream said: {KEY}"), "provider_error", "500"),
        (lambda r: httpx.Response(429, json={"error": "slow down"}), "provider_error", "429"),
        # Not followed: a redirect would carry the key to wherever it points.
        (
            lambda r: httpx.Response(302, headers={"Location": "http://evil.test/"}),
            "provider_error",
            "302",
        ),
        (lambda r: httpx.Response(200, text="not json"), "provider_bad_response", "JSON"),
        (lambda r: httpx.Response(200, json={"data": "nope"}), "provider_bad_response", "shape"),
        (lambda r: httpx.Response(200, json={"data": []}), "provider_bad_response", "count"),
        (
            lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": ["x"]}]}),
            "provider_bad_response",
            "shape",
        ),
    ],
)
async def test_a_bad_answer_is_a_provider_error_that_leaks_nothing(
    make_response: Callable[[httpx.Request], httpx.Response], code: str, words: str
) -> None:
    with pytest.raises(ProviderError) as e:
        await embeddings(make_response).embed(["a"])
    assert e.value.code == code
    assert words in str(e.value)
    assert KEY not in str(e.value) and "slow down" not in str(e.value)  # nor the response body


async def test_chat_replies_of_the_wrong_shape_are_refused() -> None:
    for body in ({"choices": []}, {"choices": [{"message": {"content": 5}}]}, {}):
        with pytest.raises(ProviderError) as e:
            await chat(lambda r, b=body: httpx.Response(200, json=b)).complete("s", "u")
        assert e.value.code == "provider_bad_response"


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_a_transport_failure_is_unreachable_and_does_not_name_the_key(
    exc: httpx.HTTPError,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(ProviderError) as e:
        await chat(handler).complete("s", "u")
    assert e.value.code == "provider_unreachable"
    assert str(e.value) == type(exc).__name__  # the kind of failure, not the exception's text


async def test_a_server_that_never_finishes_is_given_up_on_at_the_deadline() -> None:
    """httpx times each read, not the whole call, so a server that drips would never time out."""

    async def drip(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": []})

    slow = EmbeddingsClient("https://models.test/v1", "e", KEY, 0.05, client(drip))
    with pytest.raises(ProviderError, match="timeout") as e:
        await slow.embed(["a"])
    assert e.value.code == "provider_unreachable"


async def test_the_real_client_does_not_follow_redirects() -> None:
    """The mock transport tests above cannot show it: they bring their own client."""
    real = EmbeddingsClient("https://models.test/v1", "e", KEY, 5)
    try:
        assert real._client.follow_redirects is False
    finally:
        await real.aclose()


def test_nothing_is_built_unless_the_feature_is_on_and_configured() -> None:
    assert embeddings_from(Settings()) is None
    assert chat_from(Settings()) is None
    # Configured but switched off is still nothing: the flag is the switch.
    off = Settings(embedding_base_url="https://m.test/v1", embedding_model="m", llm_model="m")
    assert embeddings_from(off) is None and chat_from(off) is None


async def test_a_provider_is_built_from_settings_with_its_own_key() -> None:
    s = Settings(
        embedding_rerank=True,
        embedding_base_url="https://m.test/v1",
        embedding_model="e",
        embedding_api_key=KEY,
        llm_extraction=True,
        llm_base_url="http://localhost:9/v1",
        llm_model="c",
        llm_api_key="the-chat-key",
    )
    emb, llm = embeddings_from(s), chat_from(s)
    try:
        assert isinstance(emb, EmbeddingsClient) and isinstance(llm, ChatClient)
        assert emb._key == KEY and llm._key == "the-chat-key"  # each gets its own
        assert KEY not in repr(emb) + repr(llm)
    finally:
        assert emb is not None and llm is not None
        await emb.aclose()
        await llm.aclose()
