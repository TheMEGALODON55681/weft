"""Bytes to text for pages that lie about, or never declare, their encoding. Never raises."""

import codecs
import re

from charset_normalizer import from_bytes

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# Browsers treat these labels as windows-1252, which differs from latin-1 in 0x80-0x9f.
_LATIN1_LABELS = frozenset({"iso-8859-1", "iso8859-1", "latin1", "latin-1", "us-ascii", "ascii"})


def decode_body(body: bytes, header_charset: str | None) -> str:
    for bom, encoding in _BOMS:
        if body.startswith(bom):
            return body.decode(encoding, errors="replace")

    declared = [header_charset]
    if meta := _META_CHARSET.search(body[:4096]):
        declared.append(meta.group(1).decode("ascii", errors="ignore"))
    for label in filter(None, declared):
        encoding = "cp1252" if label.lower() in _LATIN1_LABELS else label
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if best := from_bytes(body).best():
        return str(best)
    return body.decode("utf-8", errors="replace")
