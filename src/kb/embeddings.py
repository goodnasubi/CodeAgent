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

    @property
    def max_distance(self) -> float:
        """これより遠い知識は「関連なし」として捨てる距離（コサイン距離）。

        **モデルごとに違う値であり、共通の定数には置けない。** 距離の分布は
        モデルの性質そのもので、あるモデルで「無関係」を意味する 0.85 が、
        別のモデルでは「まったく届かない値」になる。ベクトルを作った当人が
        自分の値を申告する形にしてあるのはそのため。

        足切りが無いと、どんな質問にも必ず何かが返る。無関係な結果が並ぶ
        だけでなく、**「見つからなかったので新しく登録する」という筋道に
        永久に到達できなくなる**。
        """

    @property
    def is_development(self) -> bool:
        """検索結果に意味がないことを利用者に伝えるべきプロバイダか。

        **`max_distance` と同じく、プロバイダ自身が申告する。** 呼び出し側が
        プロバイダ名を見て判定すると、判定を書いた場所すべてが新しい開発用
        実装を知らないままになる。

        これが要るのは、テナントの既定値が開発用ハッシュ実装だからである。
        鍵を設定しないまま使い始めても検索は動いてしまい、それらしい件数の
        結果まで返る。**壊れて見えないのに結果が無意味**という、最も気づき
        にくい状態なので、検索するたびに画面へ出す。
        """

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

    #: このハッシュ実装での実測は、関連ありが 0.46〜0.65、無関係が 0.88〜1.00。
    #: **この値を実プロバイダに流用しないこと。** 単語の重なりだけを見ている
    #: ため距離が全体的に大きく、意味で近い実モデルとは分布がまるで違う。
    DEFAULT_MAX_DISTANCE = 0.85

    def __init__(
        self,
        *,
        dimensions: int = 768,
        model: str = "hashing-dev",
        max_distance: float = DEFAULT_MAX_DISTANCE,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._dimensions = dimensions
        self._model = model
        self._max_distance = max_distance

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_distance(self) -> float:
        return self._max_distance

    @property
    def is_development(self) -> bool:
        return True

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
