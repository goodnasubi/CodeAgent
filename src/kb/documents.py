"""ドキュメント → Markdown 変換。

markitdown に委譲する。markitdown の出力は「テキスト解析ツールが消費する前提」で
人間向けの高忠実度変換ではないが、embedding の前処理という本用途には合致する。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markitdown import MarkItDown


class ConversionError(RuntimeError):
    """変換に失敗した。元の例外を __cause__ に持つ。"""


@dataclass(frozen=True)
class ConvertedDocument:
    source_name: str
    text: str


class DocumentConverter:
    """ファイルや URL を Markdown テキストに変換する。

    画像を含む形式の OCR は、渡された LLM クライアントに委譲される
    （テナントが選択した LLM をそのまま使う方針のため）。
    """

    def __init__(
        self,
        *,
        llm_client: Any | None = None,
        llm_model: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if llm_client is not None:
            kwargs["llm_client"] = llm_client
            if llm_model is not None:
                kwargs["llm_model"] = llm_model
        self._md = MarkItDown(**kwargs)

    def convert_path(self, path: str | Path) -> ConvertedDocument:
        p = Path(path)
        if not p.is_file():
            raise ConversionError(f"ファイルが見つかりません: {p}")
        try:
            result = self._md.convert(str(p))
        except Exception as exc:  # markitdown は形式ごとに多様な例外を投げる
            raise ConversionError(f"{p.name} を変換できませんでした") from exc
        return ConvertedDocument(source_name=p.name, text=result.text_content or "")

    def convert_bytes(self, data: bytes, *, filename: str) -> ConvertedDocument:
        """アップロードされたファイルを変換する。

        markitdown は拡張子から形式を判定するため、元の拡張子を保った
        一時ファイルに書き出してから渡す。
        """
        suffix = Path(filename).suffix
        tmp_dir = Path(tempfile.mkdtemp(prefix="kb-convert-"))
        try:
            tmp = tmp_dir / f"upload{suffix}"
            tmp.write_bytes(data)
            converted = self.convert_path(tmp)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return ConvertedDocument(source_name=filename, text=converted.text)

    def convert_url(self, url: str) -> ConvertedDocument:
        try:
            result = self._md.convert(url)
        except Exception as exc:
            raise ConversionError(f"URL を変換できませんでした: {url}") from exc
        return ConvertedDocument(source_name=url, text=result.text_content or "")
