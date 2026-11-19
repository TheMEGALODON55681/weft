"""HTTPS through the address guard. The guard connects to a pinned IP, so these prove that the
certificate is still checked against the host name in the URL, and still checked at all."""

import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from fixture_site import FixtureSite
from weft.fetch.errors import FetchError
from weft.fetch.http import Fetcher


def write_self_signed(path: Path, dns_name: str) -> None:
    """A throwaway certificate and key, valid for `dns_name` only: no IP address in it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


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
