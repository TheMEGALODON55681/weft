import io
import json
import logging

from weft.obs.logging import configure_logging, safe_url


def _emit(stream: io.StringIO, level: str = "INFO") -> logging.Logger:
    configure_logging(level, stream=stream)
    return logging.getLogger("weft.test")


def test_emits_one_json_object_per_line_with_extras() -> None:
    out = io.StringIO()
    _emit(out).info("fetched", extra={"url": "https://a.test/", "status": 200})
    rec = json.loads(out.getvalue())
    assert rec["msg"] == "fetched"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "weft.test"
    assert rec["url"] == "https://a.test/"
    assert rec["status"] == 200
    assert rec["ts"].endswith("Z")


def test_secret_fields_are_redacted_including_nested() -> None:
    out = io.StringIO()
    _emit(out).info(
        "x",
        extra={"token": "s3cret", "headers": {"Authorization": "Bearer abc", "Accept": "*/*"}},
    )
    text = out.getvalue()
    assert "s3cret" not in text
    assert "Bearer abc" not in text
    rec = json.loads(text)
    assert rec["token"] == "***"
    assert rec["headers"] == {"Authorization": "***", "Accept": "*/*"}


def test_exception_is_serialised_on_the_same_line() -> None:
    out = io.StringIO()
    log = _emit(out)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    assert "ValueError: boom" in json.loads(lines[0])["exc"]


def test_level_filters_and_reconfigure_does_not_duplicate_handlers() -> None:
    out = io.StringIO()
    configure_logging("INFO", stream=io.StringIO())
    log = _emit(out, "WARNING")
    log.info("quiet")
    log.warning("loud")
    assert [json.loads(line)["msg"] for line in out.getvalue().splitlines()] == ["loud"]


def test_safe_url_drops_credentials_and_masks_secret_params() -> None:
    url = "https://user:pw@a.test:8443/p?q=1&api_key=zzz&token=t0k&page=2#frag"
    assert safe_url(url) == "https://a.test:8443/p?q=1&api_key=***&token=***&page=2"


def test_safe_url_leaves_clean_urls_alone() -> None:
    assert safe_url("https://a.test/x?a=1") == "https://a.test/x?a=1"


def test_safe_url_keeps_ipv6_brackets_and_never_raises() -> None:
    assert safe_url("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert safe_url("http://a.test:notaport/") == "<unparseable url>"


def test_redaction_reaches_into_lists_of_dicts() -> None:
    out = io.StringIO()
    _emit(out).info("x", extra={"hops": [{"url": "u", "Cookie": "sid=1"}, ("a", {"token": "t"})]})
    assert "sid=1" not in out.getvalue()
    assert '"t"' not in out.getvalue()
    assert json.loads(out.getvalue())["hops"][0] == {"url": "u", "Cookie": "***"}
