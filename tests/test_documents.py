"""ファイル・URL の取り込み。

外部に出るのは `convert_url` の実 HTTP だけで、それは testable な形に
分けられないので触らない。ここで確かめたいのは**入力の門番**の方。
"""

import base64
from pathlib import Path

import pytest

from kb.documents import (
    MAX_SOURCE_BYTES,
    ConversionError,
    DocumentConverter,
    UnsupportedSource,
)
from kb.providers import ProviderError


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


# --------------------------------------------------------- 画像の文字起こし


class FakeLlm:
    """LlmClient の代役。呼ばれたかどうかも見たいので記録する。"""

    model = "fake"

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[int, str]] = []

    def extract_text_from_image(self, data: bytes, *, mime_type: str) -> str:
        self.calls.append((len(data), mime_type))
        if self.error is not None:
            raise self.error
        return self.text


PNG = (Path(__file__).parent / "data" / "ocr-sample.png").read_bytes()


def test_image_goes_to_the_llm_not_markitdown():
    """markitdown の画像変換は Exif を返すだけで、文字起こしにはならない。"""
    llm = FakeLlm("ORA-01555: snapshot too old")
    doc = DocumentConverter(llm=llm).convert_bytes(PNG, filename="error.png")

    assert doc.source_name == "error.png"
    assert "ORA-01555" in doc.text
    assert llm.calls == [(len(PNG), "image/png")]


def test_image_without_an_llm_does_not_fail():
    """LLM 未設定でも 500 にはしない。文字が取れないだけ。"""
    doc = DocumentConverter().convert_bytes(PNG, filename="error.png")
    assert "ORA-01555" not in doc.text


def test_unsupported_image_format_does_not_reach_the_llm():
    """gif は Gemini が受け付けない形式。**LLM には渡さず** markitdown に回す。

    markitdown も gif を扱えないので、結果は「変換できない」になる。
    無駄に API を叩いて 400 をもらうより、手前で分かれる方がよい。
    """
    gif = base64.b64decode(
        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    )
    llm = FakeLlm("呼ばれてはいけない")
    with pytest.raises(ConversionError):
        DocumentConverter(llm=llm).convert_bytes(gif, filename="x.gif")
    assert llm.calls == []


def test_llm_failure_becomes_a_conversion_error():
    llm = FakeLlm(error=ProviderError("レート制限"))
    with pytest.raises(ConversionError, match="文字起こし"):
        DocumentConverter(llm=llm).convert_bytes(PNG, filename="error.png")


def test_non_image_ignores_the_llm():
    llm = FakeLlm("呼ばれてはいけない")
    doc = DocumentConverter(llm=llm).convert_bytes(b"ORA-01555", filename="log.txt")
    assert "ORA-01555" in doc.text
    assert llm.calls == []
