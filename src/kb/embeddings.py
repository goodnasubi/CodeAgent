"""embedding プロバイダの抽象。

LLM・embedding はテナントごとに選択できるため、呼び出し側は具体的な
プロバイダを知らない。生成に使ったモデル名と次元数は、ベクトルと一緒に
必ず保存する（異なるモデルのベクトルは比較できないため）。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str:
        """モデル識別子。ベクトルと一緒に保存される。"""

    @property
    def dimensions(self) -> int:
        """出力ベクトルの次元数。"""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """テキスト列をベクトル化する。入力と同じ順序・同じ長さで返す。"""


_TOKEN = re.compile(r"[0-9A-Za-z_]+|[぀-ヿ一-鿿]")


def _tokenize(text: str) -> list[str]:
    """粗いトークン分割。

    英数字は単語単位、日本語は 1 文字単位で切る。日本語の形態素解析は
    行わない（ダミー実装の用途には過剰なため）。
    """
    return _TOKEN.findall(text.lower())


class HashingEmbeddingProvider:
    """ハッシュベースの決定的な embedding。

    API キーなしで開発とテストを進めるためのもの。**本番では使わない。**
    語彙を共有するテキストが近いベクトルになるよう bag-of-words を
    ハッシュ空間に写像しているため、「似た文書が見つかること」を
    検証する E2E テストが書ける。意味的な類似（言い換え）は捉えない。
    """

    def __init__(self, *, dimensions: int = 768, model: str = "hashing-dev") -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._dimensions = dimensions
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dimensions
        for token in _tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            bucket = value % self._dimensions
            # 符号を散らし、頻出語だけで方向が決まらないようにする
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vec[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # 空文字やトークンを持たない入力。ゼロベクトルはコサイン距離が
            # 定義できないため、決定的な単位ベクトルを返す
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]
