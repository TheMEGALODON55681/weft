"""Optional model providers: embeddings for reranking and a chat model for extraction.

Both are off by default. Each is a Protocol, with one implementation here that speaks the
chat-completions and embeddings HTTP API most model servers offer, so a hosted service and a local
server differ only in the base URL. The endpoint comes from settings and never from a tool
argument.

A call fails in one of three ways, each a `ProviderError` with a stable code. The message never
holds the key or the response body.

- `provider_unreachable`: no complete answer within `provider_timeout_s`, or the connection
  failed.
- `provider_error`: an HTTP status that is not 2xx. Redirects are not followed, since one would
  carry the key to wherever it points.
- `provider_bad_response`: a 2xx answer that is not what the API promises.

Callers treat all three as "carry on without the model".
"""

import asyncio
from typing import Any, Protocol

import httpx

from weft.config import Settings


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order."""
        ...


class LlmProvider(Protocol):
    async def complete(self, system: str, user: str) -> str:
        """The model's reply to a system and a user message."""
        ...


class _Endpoint:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._timeout_s = timeout_s
        # Not followed: see the module docstring. Proxy and certificate variables apply, because
        # an operator who needs a proxy to reach a hosted API sets them.
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._owns_client = client is None

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            # httpx's timeouts apply per read, so a server that drips bytes could hold the call
            # open. This is the wall-clock limit.
            async with asyncio.timeout(self._timeout_s):
                response = await self._client.post(
                    f"{self._base}/{path}", json=payload, headers=headers
                )
        except TimeoutError:
            raise ProviderError("provider_unreachable", "timeout") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ProviderError("provider_unreachable", type(exc).__name__) from None
        if not response.is_success:
            raise ProviderError("provider_error", f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("provider_bad_response", "response is not JSON") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class EmbeddingsClient(_Endpoint):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = await self._post("embeddings", {"model": self._model, "input": texts})
        try:
            rows = sorted(body["data"], key=lambda row: row.get("index", 0))  # a stable sort
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError(
                "provider_bad_response", "response has an unexpected shape"
            ) from None
        if len(vectors) != len(texts):
            raise ProviderError(
                "provider_bad_response", "embedding count does not match the inputs"
            )
        return vectors


class ChatClient(_Endpoint):
    async def complete(self, system: str, user: str) -> str:
        body = await self._post(
            "chat/completions",
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                "provider_bad_response", "response has an unexpected shape"
            ) from None
        if not isinstance(content, str):
            raise ProviderError("provider_bad_response", "response has an unexpected shape")
        return content


def embeddings_from(settings: Settings) -> EmbeddingsClient | None:
    """The embeddings client, if the feature is on. The flag is the switch."""
    if not settings.embedding_rerank:
        return None
    key = settings.embedding_api_key
    return EmbeddingsClient(
        settings.embedding_base_url,
        settings.embedding_model,
        key.get_secret_value() if key else None,
        settings.provider_timeout_s,
    )


def chat_from(settings: Settings) -> ChatClient | None:
    """The chat client, if the feature is on. The flag is the switch."""
    if not settings.llm_extraction:
        return None
    key = settings.llm_api_key
    return ChatClient(
        settings.llm_base_url,
        settings.llm_model,
        key.get_secret_value() if key else None,
        settings.provider_timeout_s,
    )
