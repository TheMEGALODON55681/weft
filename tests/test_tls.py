"""HTTPS through the address guard. The guard connects to a pinned IP, so these prove that the
certificate is still checked against the host name in the URL, and still checked at all."""

import ssl
from collections.abc import Callable
from pathlib import Path

import pytest

from certs import write_self_signed
from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher


@pytest.fixture
def https_site(
    tmp_path: Path, make_site: Callable[..., FixtureSite], monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], FixtureSite]:
    """An HTTPS fixture site whose certificate names `dns_name`, trusted through SSL_CERT_FILE."""

    def start(dns_name: str) -> FixtureSite:
        pem = tmp_path / "site.pem"
        write_self_signed(pem, dns_name)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pem)
        monkeypatch.setenv("SSL_CERT_FILE", str(pem))
        return make_site(tls=context)

    return start


def by_name(site: FixtureSite, path: str) -> str:
    return site.url(path).replace("127.0.0.1", "localhost")


async def test_https_is_verified_against_the_name_not_the_pinned_address(
    https_site: Callable[[str], FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = https_site("localhost")
    # The socket goes to 127.0.0.1 or ::1. The certificate only says "localhost".
    r = await fetch_with().fetch(by_name(site, "/article"))
    assert r.status == 200
    assert r.final_url.startswith("https://localhost:")


async def test_a_certificate_for_another_name_is_refused(
    https_site: Callable[[str], FixtureSite], fetch_with: Callable[..., Fetcher]
) -> None:
    site = https_site("other.test")
    with pytest.raises(FetchError) as e:
        await fetch_with(max_retries=0).fetch(by_name(site, "/article"))
    assert e.value.code == "network_error"
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value)
