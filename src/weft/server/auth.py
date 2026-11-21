"""Bearer-token auth for the HTTP transport: one shared secret, from the environment."""

import hashlib
import hmac
import logging

from fastmcp.server.auth import AccessToken, TokenVerifier

log = logging.getLogger("weft.server")


def _digest(token: str) -> bytes:
    # Hashing first gives both sides the same length, so a wrong length leaks nothing, and lets
    # any text through: compare_digest refuses a str that is not ASCII.
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


class BearerToken(TokenVerifier):
    """Accepts exactly one token. The SDK's bearer middleware answers 401 with a `WWW-Authenticate`
    challenge for a missing, malformed or rejected one, so `verify_token` only decides."""

    def __init__(self, token: str) -> None:
        super().__init__()
        if not token:
            raise ValueError("an empty token would admit any caller")
        self._expected = _digest(token)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(_digest(token), self._expected):
            log.warning("bearer token rejected")  # never the token itself, or even its length
            return None
        return AccessToken(token=token, client_id="weft", scopes=[])
