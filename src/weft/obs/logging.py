"""JSON-lines logging to stderr.

stdout belongs to the MCP stdio transport, so nothing here may ever write to it.
Secrets are redacted by field name; put sensitive values in `extra`, never in the message.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit

_SENSITIVE = re.compile(
    r"token|secret|passw|auth|cookie|session|signature|api[_-]?key|^key$|_key$", re.I
)
# uvicorn puts an ANSI-coloured copy of each message in `color_message`.
_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE.search(key):
        return "***"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        doc: dict[str, Any] = {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                doc[key] = _redact(value, key)
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Attach a single JSON handler to the `weft` logger and to the loggers of the HTTP stack, so
    that stderr holds one format. Safe to call repeatedly."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonFormatter())
    for name in ("weft", "uvicorn", "fastmcp", "mcp"):
        logger = logging.getLogger(name)
        for old in list(logger.handlers):
            logger.removeHandler(old)
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False


def safe_url(url: str) -> str:
    """URL fit for a log line: no credentials, no fragment, secret query values masked."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        return "<unparseable url>"
    query = urlencode(
        [(k, "***" if _SENSITIVE.search(k) else v) for k, v in parse_qsl(parts.query, True)],
        safe="*",
    )
    return f"{parts.scheme}://{host}{parts.path}" + (f"?{query}" if query else "")
