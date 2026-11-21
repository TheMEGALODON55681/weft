"""Structured extraction by rule: CSS and XPath selectors written into a JSON Schema.

The caller's schema is an ordinary JSON Schema. Where a value lives on the page is said with
three extra keywords, which JSON Schema ignores as unknown annotations:

    x-css    a CSS selector
    x-xpath  an XPath 1.0 expression (use one of the two, not both)
    x-attr   take this attribute of the match instead of its text; href and src are made absolute

A scalar takes the first match. An array takes every match, and when its items are objects each
match is a record whose properties are found inside it, so their selectors are relative. An object
with a rule is scoped to its first match, and one without keeps the scope it is in. A property
without a rule is not extracted. `Extractor.run` builds the data, and `Extractor.errors` says why
it does not satisfy the schema, which is how a caller (or an LLM fallback) knows the rules failed.

The rules read the HTML, so they see the rendered DOM when the page went through the browser. All
selectors run on one lxml tree, which lets CSS and XPath mix freely in one schema: a record found
by CSS can have fields found by XPath. `$ref` is followed only inside the schema itself.
"""

import itertools
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import jsonschema
from cssselect import SelectorError
from lxml import etree
from lxml import html as lxml_html
from lxml.cssselect import CSSSelector

MAX_NODES = 300  # properties and items in one schema that carry or inherit rules
MAX_DEPTH = 8  # levels of object and array a rule can nest
MAX_SELECTOR = 500
MAX_SCHEMA_CHARS = 50_000  # the schema serialised as compact JSON
MAX_DATA_CHARS = 100_000  # the text one run may collect, by default
_MAX_JSON_DEPTH = 64
_MAX_ERRORS = 10
_MESSAGE_CHARS = 300
_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")
_RULE_KEYS = ("x-css", "x-xpath", "x-attr")
_KINDS = frozenset({"string", "integer", "number", "boolean", "array", "object"})
_NUMBER = re.compile(r"-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)")
_MISSING: Any = object()


class InvalidSchema(ValueError):
    """The schema, or a rule in it, cannot be used. The message says where."""


class DataTooLarge(ValueError):
    """The rules matched more text than the caller allowed. A selector for `div` on a page of
    nested divs matches the same text once per level, so a small page can yield a huge answer."""


@dataclass(slots=True)
class _Budget:
    limit: int
    left: int

    def spend(self, chars: int) -> None:
        self.left -= chars
        if self.left < 0:
            raise DataTooLarge(f"the rules found more than {self.limit} characters; narrow them")


@dataclass(slots=True)
class _Rule:
    kind: str
    find: Callable[[Any], Any] | None = None  # scope element -> matches
    attr: str | None = None
    props: dict[str, "_Rule"] = field(default_factory=dict)
    item: "_Rule | None" = None  # what an array holds


_LEAF = _Rule("string")  # an array item with nothing said about it


def _kind(schema: dict[str, Any]) -> str:
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), None)
    if declared in _KINDS:
        return str(declared)
    if "properties" in schema:
        return "object"
    return "array" if "items" in schema else "string"


def _check_structure(schema: Any) -> None:
    """Refuse a reference that leaves the schema, and absurd nesting. Iterative, so that nesting
    cannot overflow the stack here or in the validator."""
    stack = [(schema, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise InvalidSchema("schema is nested too deep")
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _REF_KEYS and isinstance(value, str) and not value.startswith("#"):
                    raise InvalidSchema(
                        f"{key} {value!r} points outside the schema: only '#...' references work"
                    )
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)


def _number(raw: Any) -> int | float | None:
    if isinstance(raw, (int, float)):
        n = float(raw)
    else:
        found = _NUMBER.search(str(raw))
        if found is None:
            return None
        n = float(found.group().replace(",", ""))
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n


def _value(rule: _Rule, match: Any, base: str) -> Any:
    """One scalar from one match, or _MISSING."""
    raw: Any
    if isinstance(match, (bool, int, float, str)):  # an XPath result: count(), @href, text()
        raw = match
    elif rule.attr is not None:
        raw = match.get(rule.attr)
        if raw is not None and rule.attr in ("href", "src"):
            raw = urljoin(base, raw.strip())
    elif hasattr(match, "text_content"):
        raw = match.text_content()
    else:  # a comment or a processing instruction
        return _MISSING
    if raw is None:
        return _MISSING
    if rule.kind in ("integer", "number"):
        n = _number(raw)
        return _MISSING if n is None else n
    text = " ".join(str(raw).split())
    return text or _MISSING


def _nothing(value: Any) -> bool:
    """A value that says nothing was found: absent, or a record with no fields."""
    return value is _MISSING or (isinstance(value, dict) and not value)


def _parse(html: str) -> Any:
    if not html.strip():
        return None
    try:
        # Bytes with a declared encoding: a str that starts with an XML declaration is refused.
        parser = lxml_html.HTMLParser(encoding="utf-8")
        return lxml_html.document_fromstring(html.encode("utf-8", errors="replace"), parser=parser)
    except (etree.ParserError, ValueError):
        return None


class Extractor:
    """A schema with its rules compiled. Raises InvalidSchema at construction, so a bad schema
    fails before anything is fetched. Safe to share: lxml serialises use of one compiled
    selector across threads."""

    def __init__(self, schema: dict[str, Any]) -> None:
        _check_structure(schema)  # first: it bounds the depth that dumps and check_schema walk
        if len(json.dumps(schema, separators=(",", ":"))) > MAX_SCHEMA_CHARS:
            raise InvalidSchema(f"schema is too large (over {MAX_SCHEMA_CHARS} characters)")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise InvalidSchema(f"not a valid JSON Schema: {exc.message}") from None
        except RecursionError:
            raise InvalidSchema("schema is nested too deep") from None
        self._schema = schema
        self._validator = jsonschema.Draft202012Validator(schema)
        self._nodes = 0
        self.has_rules = False
        self._root = self._compile(schema, "", 0)

    def _find(self, path: str, s: dict[str, Any]) -> Callable[[Any], Any] | None:
        where = path or "(root)"
        css, xpath, attr = s.get("x-css"), s.get("x-xpath"), s.get("x-attr")
        for key, given in (("x-css", css), ("x-xpath", xpath), ("x-attr", attr)):
            if given is not None and not isinstance(given, str):
                raise InvalidSchema(f"{where}: {key} must be a string")
        if css is not None and xpath is not None:
            raise InvalidSchema(f"{where}: x-css and x-xpath are both set; use one")
        selector = css if css is not None else xpath
        if selector is None:
            if attr is not None:
                raise InvalidSchema(f"{where}: x-attr needs x-css or x-xpath")
            return None
        if len(selector) > MAX_SELECTOR:
            raise InvalidSchema(f"{where}: selector is too long (over {MAX_SELECTOR} characters)")
        try:
            if css is not None:
                compiled: Callable[[Any], Any] = CSSSelector(css, translator="html")
            else:
                compiled = etree.XPath(selector)
        except (SelectorError, etree.XPathError, ValueError) as exc:
            kind = "CSS selector" if css is not None else "XPath"
            raise InvalidSchema(f"{where}: not a valid {kind} ({exc})") from None
        self.has_rules = True
        return compiled

    def _compile(self, s: Any, path: str, depth: int) -> _Rule:
        where = path or "(root)"
        if depth > MAX_DEPTH:
            raise InvalidSchema(f"{where}: schema is nested too deep (over {MAX_DEPTH} levels)")
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise InvalidSchema(f"schema is too large (over {MAX_NODES} properties)")
        if not isinstance(s, dict):  # `true` or `false`: nothing to look for
            return _Rule("string")
        rule = _Rule(_kind(s), self._find(path, s), s.get("x-attr"))
        if rule.kind == "object" and isinstance(s.get("properties"), dict):
            for name, sub in s["properties"].items():
                rule.props[name] = self._compile(
                    sub, f"{path}/properties/{name}".lstrip("/"), depth + 1
                )
        elif rule.kind == "array":
            rule.item = self._compile(s.get("items"), f"{path}/items".lstrip("/"), depth + 1)
        return rule

    def run(self, html: str, base_url: str, max_chars: int = MAX_DATA_CHARS) -> Any:
        """The data the rules find in `html`, or None if there is no document. Whatever is not
        found is left out, so the result may not satisfy the schema: ask `errors`. Raises
        DataTooLarge once the values hold more than `max_chars` characters."""
        doc = _parse(html)
        if doc is None:
            return None
        found = self._eval(self._root, doc, base_url, False, _Budget(max_chars, max_chars))
        return None if found is _MISSING else found

    @staticmethod
    def _matches(find: Callable[[Any], Any], scope: Any) -> list[Any]:
        try:
            found = find(scope)
        except etree.XPathError:  # a failure while evaluating is no match
            return []
        return found if isinstance(found, list) else [found]

    def _eval(self, rule: _Rule, scope: Any, base: str, as_item: bool, budget: _Budget) -> Any:
        find = rule.find
        if find is None and rule.kind != "object" and not as_item:
            return _MISSING  # nothing says where this value is
        matches = [scope] if find is None else self._matches(find, scope)
        if rule.kind == "object":
            inner = scope if find is None else (matches[0] if matches else None)
            if inner is None:
                return _MISSING
            record = {}
            for name, child in rule.props.items():
                value = self._eval(child, inner, base, False, budget)
                if not _nothing(value):
                    record[name] = value
            return record
        if rule.kind == "array":
            items = (self._eval(rule.item or _LEAF, m, base, True, budget) for m in matches)
            return [v for v in items if not _nothing(v)]
        if rule.kind == "boolean":
            budget.spend(1)
            return bool(matches)
        for m in matches:
            value = _value(rule, m, base)
            if value is not _MISSING:
                budget.spend(len(str(value)))
                return value
        return _MISSING

    def errors(self, data: Any) -> list[str]:
        """Why `data` does not satisfy the schema: at most ten, each short, as 'path: problem'.
        Empty when it does."""
        found = itertools.islice(self._validator.iter_errors(data), 100)
        ordered = sorted(found, key=lambda e: [str(p) for p in e.absolute_path])
        return [
            f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"[:_MESSAGE_CHARS]
            for e in ordered[:_MAX_ERRORS]
        ]

    def prompt_schema(self) -> Any:
        """The schema without its rules, for a reader that has no use for selectors."""

        def strip(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: strip(v) for k, v in node.items() if k not in _RULE_KEYS}
            if isinstance(node, list):
                return [strip(v) for v in node]
            return node

        return strip(self._schema)
