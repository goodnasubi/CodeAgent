"""ファイル・URL の取り込み。

外部に出るのは `convert_url` の実 HTTP だけで、それは testable な形に
分けられないので触らない。ここで確かめたいのは**入力の門番**の方。
"""

import pytest

from kb.documents import (
    MAX_SOURCE_BYTES,
    ConversionError,
    DocumentConverter,
    UnsupportedSource,
)


@pytest.fixture
def converter() -> DocumentConverter:
    return DocumentConverter()


def test_plain_text_is_converted(converter):
    doc = converter.convert_bytes("ORA-01555 が出ました".encode(), filename="log.txt")

    assert doc.source_name == "log.txt"
    assert "ORA-01555" in doc.text


def test_excel_becomes_text(converter, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.append(["症状", "対処"])
    book.active.append(["接続できない", "MTU を下げる"])
    path = tmp_path / "症状一覧.xlsx"
    book.save(path)

    doc = converter.convert_bytes(path.read_bytes(), filename="症状一覧.xlsx")

    assert "接続できない" in doc.text
    assert "MTU を下げる" in doc.text


def test_extension_decides_the_format_not_the_path(converter, tmp_path):
    """名前にパス区切りが混ざっていても、一時ディレクトリの外には出ない。"""
    doc = converter.convert_bytes(b"hello", filename="../../../etc/passwd.txt")

    assert "hello" in doc.text
    assert doc.source_name == "../../../etc/passwd.txt"  # 表示名は受け取ったまま


def test_oversized_upload_is_refused(converter):
    with pytest.raises(UnsupportedSource):
        converter.convert_bytes(b"x" * (MAX_SOURCE_BYTES + 1), filename="big.txt")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hostname",  # これを通すとサーバー上の任意のファイルが読める
        "/etc/hostname",
        "data:text/plain;base64,aGVsbG8=",
        "ftp://example.com/x.txt",
    ],
)
def test_only_http_urls_are_accepted(converter, url):
    with pytest.raises(UnsupportedSource):
        converter.convert_url(url)


def test_missing_file_is_a_conversion_error(converter, tmp_path):
    with pytest.raises(ConversionError):
        converter.convert_path(tmp_path / "ない.txt")
