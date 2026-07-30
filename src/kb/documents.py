"""ドキュメント → Markdown 変換。

markitdown に委譲する。markitdown の出力は「テキスト解析ツールが消費する前提」で
人間向けの高忠実度変換ではないが、embedding の前処理という本用途には合致する。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from markitdown import MarkItDown

from .llm import LlmClient
from .providers import ProviderError

#: 受け付ける最大サイズ。1 ファイルが数千チャンクに化けると embedding 費用が跳ねる
MAX_SOURCE_BYTES = 20 * 1024 * 1024

#: 拡張子から画像の MIME を引く。ここに挙げた形式だけ LLM に文字起こしさせ、
#: 残り（gif / bmp / tiff など）は markitdown に任せる。
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

#: URL 取り込みで許す scheme。**file:// を通すとサーバー上の任意のファイルが
#: 読めてしまう**（markitdown は実際に /etc/hostname を返す）ため、素通しにしない。
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


class ConversionError(RuntimeError):
    """変換に失敗した。元の例外を __cause__ に持つ。"""


class UnsupportedSource(ValueError):
    """入力そのものを受け付けない（大きすぎる、scheme が許されていない等）。"""


@dataclass(frozen=True)
class ConvertedDocument:
    source_name: str
    text: str


class DocumentConverter:
    """ファイルや URL を Markdown テキストに変換する。

    画像の文字起こしは、テナントが選んだ LLM に委譲する。**markitdown の
    `llm_client` フックは使わない**。markitdown は OpenAI 形式の
    `client.chat.completions.create` を直接呼ぶため他社のクライアントを
    そのまま渡せず、しかも既定のプロンプトが「画像の説明文」を作らせるもので、
    文字起こしという用途と食い違うため。
    """

    def __init__(self, *, llm: LlmClient | None = None) -> None:
        self._llm = llm
        self._md = MarkItDown()

    def convert_path(self, path: str | Path) -> ConvertedDocument:
        p = Path(path)
        if not p.is_file():
            raise ConversionError(f"ファイルが見つかりません: {p}")

        mime = IMAGE_MIME_TYPES.get(p.suffix.lower())
        if mime is not None and self._llm is not None:
            return ConvertedDocument(
                source_name=p.name, text=self._extract_image(p.read_bytes(), mime, p.name)
            )

        try:
            result = self._md.convert(str(p))
        except Exception as exc:  # markitdown は形式ごとに多様な例外を投げる
            raise ConversionError(f"{p.name} を変換できませんでした") from exc
        return ConvertedDocument(source_name=p.name, text=result.text_content or "")

    def _extract_image(self, data: bytes, mime_type: str, name: str) -> str:
        try:
            return self._llm.extract_text_from_image(data, mime_type=mime_type)
        except ProviderError as exc:
            raise ConversionError(f"{name} の文字起こしに失敗しました: {exc}") from exc

    def convert_bytes(self, data: bytes, *, filename: str) -> ConvertedDocument:
        """アップロードされたファイルを変換する。

        markitdown は拡張子から形式を判定するため、元の拡張子を保った
        一時ファイルに書き出してから渡す。
        """
        if len(data) > MAX_SOURCE_BYTES:
            raise UnsupportedSource(
                f"{filename} が大きすぎます（上限 {MAX_SOURCE_BYTES // (1024 * 1024)}MB）"
            )
        # 受け取った名前はそのまま使わない。パス区切りを含んでいれば一時ディレクトリの
        # 外に書けてしまう
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
        scheme = urlparse(url).scheme.lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            raise UnsupportedSource(
                f"{scheme or '(scheme なし)'} は受け付けません。http か https を指定してください"
            )
        try:
            result = self._md.convert(url)
        except Exception as exc:
            raise ConversionError(f"URL を変換できませんでした: {url}") from exc
        return ConvertedDocument(source_name=url, text=result.text_content or "")
