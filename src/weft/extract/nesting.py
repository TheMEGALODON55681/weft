"""A cap on how deeply a document may nest, applied before it reaches the HTML parser.

lexbor, the parser behind selectolax, takes time quadratic in nesting depth: 0.1 s at 10,000
levels, 1.3 s at 20,000, about 30 s at 100,000, and a page is parsed three times. The parser
releases the GIL, so the event loop stays free, but the thread running it cannot be stopped and
there are four of them. Browsers stop at 512 levels. This cuts a document where more than
`MAX_NESTING` elements are open at once, which no real page reaches.

It is a scan, not a parser. It counts start tags and takes one off for each end tag that matches
a name it has counted, so it never needs to know what the real parser would do with misnested or
stray tags, and a stray end tag cannot get it ahead of the opens. It is a guard against pages
that nest by accident or in the obvious way. A page written to defeat this particular scan can
still get past it (an element opened in a context the scan skips, or end tags hidden in a
comment, which the scan reads as end tags). The complete answer is extraction in a child process
that can be killed, which the architecture notes list as open.
"""

import re
from collections import Counter

MAX_NESTING = 5_000

# A tag name, the text up to the next "<" or ">", and the ">" if it is there. Stopping at the next
# "<" keeps the scan linear on a document full of tags that never end.
_TAG = re.compile(r"<(/?)([A-Za-z][^\s/<>]*)([^<>]*)(>?)")
# Elements that never hold anything, and elements whose end tag is optional, so a page may leave
# any number of them open without nesting. Neither is counted.
_UNCOUNTED = frozenset(
    {
        # void
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
        # optional end tag, and the document's own three
        "html", "head", "body", "p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody",
        "tfoot", "colgroup", "option", "optgroup", "caption", "rt", "rp",
    }
)  # fmt: skip
# SVG shapes written as `<path ... />`. In SVG that closes the element. Anywhere else the HTML
# parser would open it, so this is the one gap in the count, and the reason it is a list of names
# and not every tag that ends in "/>".
_SVG_LEAVES = frozenset(
    {"path", "circle", "ellipse", "line", "polyline", "polygon", "rect", "stop", "use", "image"}
    | {"set", "animate"}
)


def cap_nesting(html: str, limit: int = MAX_NESTING) -> str:
    """`html`, or its start up to the first tag that would be the (limit + 1)th open element."""
    open_by_name: Counter[str] = Counter()
    open_now = 0
    for m in _TAG.finditer(html):
        name = m[2].lower()
        if name in _UNCOUNTED:
            continue
        if m[1]:
            if open_by_name[name] > 0:
                open_by_name[name] -= 1
                open_now -= 1
        elif not (name in _SVG_LEAVES and m[4] and m[3].endswith("/")):
            open_by_name[name] += 1
            open_now += 1
            if open_now > limit:
                return html[: m.start()]
    return html
