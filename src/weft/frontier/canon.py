"""URL canonicalisation: one spelling per page, so the frontier and the cache see one key.

What changes: scheme and host case, a trailing dot on the host, IDN hosts (to punycode), default
ports, dot segments, percent-encoding (unreserved characters decoded, the rest upper case),
query parameter order, tracking parameters, and the fragment. What does not: path case, a
trailing slash, `www.`, and the encoding of reserved characters. Each of those can change what a
server returns.
"""

import ipaddress
import re
from fnmatch import fnmatchcase
from urllib.parse import quote, urlsplit, urlunsplit

# Parameters that only say where a click came from. Keys are compared in lower case.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING = frozenset(
    {"gclid", "dclid", "fbclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "_ga", "_gl"}
)
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")
# Characters kept as they are when quoting: reserved, unreserved and "%" itself.
_SAFE = "!$&'()*+,;=:@/?-._~%"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")  # letters, digits, "-" and "_", no empty label


def _pct(text: str) -> str:
    """Decode escapes of unreserved characters, upper-case the rest, quote anything unsafe."""

    def fix(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    return quote(_PCT.sub(fix, text), safe=_SAFE)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4."""
    out: list[str] = []
    segments = path.split("/")
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == ".":
            if last:
                out.append("")
        elif seg == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(seg)
    result = "/".join(out)
    return result if result.startswith("/") else "/" + result


def _host(raw: str) -> str:
    """Lower case, no trailing dot, IDN as punycode. Raises ValueError for anything that cannot be
    the name of a host: a label IDNA cannot encode, an empty label, a space, a control character or
    a percent escape. Such a URL would be queued, refused by the fetcher and recorded as a failure,
    for no gain. An IPv6 literal is checked as an address."""
    host = raw.lower().rstrip(".")
    if ":" in host:
        if "%" in host:
            raise ValueError("a zone id in the host")
        ipaddress.IPv6Address(host)  # AddressValueError is a ValueError
        return host
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"not a usable host name: {raw!r}") from None
    if not _HOST.fullmatch(host):
        raise ValueError(f"not a usable host name: {raw!r}")
    return host


def _is_tracking(pair: str) -> bool:
    key = pair.partition("=")[0].lower()
    return key in _TRACKING or key.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Return the canonical form of an absolute http(s) URL. Raises ValueError for anything
    else, so callers can drop links they cannot crawl."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"not an absolute http(s) URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials in the URL")
    host = _host(parts.hostname)
    if ":" in host:  # an IPv6 literal
        host = f"[{host}]"
    port = parts.port  # raises ValueError for a bad port
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = _pct(_remove_dot_segments(parts.path or "/"))
    pairs = [_pct(p) for p in parts.query.split("&") if p and not _is_tracking(p)]
    return urlunsplit((scheme, netloc, path, "&".join(sorted(pairs)), ""))


def site_key(host: str) -> str:
    """Hosts that count as one site for same_domain_only: case and a leading `www.` ignored."""
    return host.lower().rstrip(".").removeprefix("www.")


_DIGITS = re.compile(r"\d+")


def pattern(url: str) -> str:
    """The shape of a URL: digits folded and query values dropped. `/cal/2031/05?d=3` and
    `/cal/2032/11?d=9` share one. A run of links that all share their parent's shape is how
    calendar and pagination traps look."""
    parts = urlsplit(url)
    keys = sorted({p.partition("=")[0] for p in parts.query.split("&") if p})
    return f"{parts.netloc}{_DIGITS.sub('N', parts.path)}?{'&'.join(keys)}"


def url_matches(url: str, include: list[str], exclude: list[str]) -> bool:
    """Whether a URL passes the glob patterns of a crawl or a search: it must match one of
    `include` when there are any, and none of `exclude`. Patterns are case-sensitive and run over
    the whole URL, query string included."""
    if include and not any(fnmatchcase(url, pat) for pat in include):
        return False
    return not any(fnmatchcase(url, pat) for pat in exclude)


def repeats_segments(url: str, limit: int = 2) -> bool:
    """True when one path segment appears more than `limit` times: `/a/b/a/b/a/b` style loops
    from relative links."""
    segs = [s for s in urlsplit(url).path.split("/") if s]
    return any(segs.count(s) > limit for s in set(segs))
