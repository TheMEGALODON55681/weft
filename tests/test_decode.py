from weft.extract.decode import decode_body


def test_utf8_by_default() -> None:
    assert decode_body("héllo".encode(), None) == "héllo"


def test_header_charset_wins() -> None:
    assert decode_body("café".encode("iso-8859-2"), "iso-8859-2") == "café"


def test_latin1_label_means_windows_1252_like_browsers() -> None:
    assert decode_body(b"\x93hi\x94", "iso-8859-1") == "“hi”"


def test_meta_charset_is_sniffed_when_header_is_silent() -> None:
    html = '<html><head><meta charset="windows-1251"></head><body>Привет</body></html>'
    assert "Привет" in decode_body(html.encode("windows-1251"), None)


def test_http_equiv_meta_is_sniffed_too() -> None:
    html = '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">Привет'
    assert "Привет" in decode_body(html.encode("windows-1251"), None)


def test_utf8_bom_is_stripped() -> None:
    assert decode_body(b"\xef\xbb\xbfhello", None) == "hello"


def test_utf16_bom_is_honoured() -> None:
    assert decode_body("hello".encode("utf-16"), None) == "hello"


def test_unknown_header_charset_is_ignored() -> None:
    assert decode_body("héllo".encode(), "no-such-codec") == "héllo"


def test_a_header_charset_that_is_not_a_codec_name_at_all_is_ignored() -> None:
    # bytes.decode raises ValueError, not LookupError, for a codec name with a null byte in it.
    assert decode_body("héllo".encode(), "utf\x00-8") == "héllo"


def test_undeclared_legacy_encoding_is_detected() -> None:
    text = "Это достаточно длинный русский текст, чтобы определить кодировку. " * 5
    assert "кодировку" in decode_body(text.encode("windows-1251"), None)


def test_garbage_never_raises() -> None:
    out = decode_body(bytes(range(256)) * 4, "utf-8")
    assert isinstance(out, str)
