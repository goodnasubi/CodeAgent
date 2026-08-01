"""Gemini（Google AI Studio の API キー方式）による embedding と画像文字起こし。

Vertex AI ではなく `generativelanguage.googleapis.com` を叩く。API キー 1 本で
完結し、GCP プロジェクトやサービスアカウントを要求しないため。Vertex に
移すときは base_url と認証だけを差し替えれば済むよう、他は分けてある。

SDK（google-genai）は使わず httpx で直接呼ぶ。バックエンド 4 種と同じ流儀で、
使うのが 2 エンドポイントだけであること、依存を増やさないことによる。
"""

from __future__ import annotations

import base64
import math
from typing import Any, Iterator, Sequence

import httpx

from .base import ProviderError

_API = "https://generativelanguage.googleapis.com/v1beta"

#: embedding の既定モデル。
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"

#: 画像文字起こしの既定モデル。
#:
#: **`gemini-2.5-flash` は使えない**。ListModels には今も出てくるが、新しく
#: 発行した API キーで generateContent を叩くと 404 と
#: 「no longer available to new users」が返る。一覧にあることは使えることを
#: 意味しないので、モデル名を変えたら実機テストで確かめること。
#:
#: `gemini-flash-latest` も同じ画像で通ったが、別名は指す先が黙って動く。
#: 版を固定した名前を選んでいる。
DEFAULT_LLM_MODEL = "gemini-3.6-flash"

#: API が受け付ける出力次元数の範囲。
MIN_DIMENSIONS = 128
MAX_DIMENSIONS = 3072

#: pgvector の HNSW インデックスが張れる上限。**これを超える次元数は
#: 検索インデックスを作れない**（halfvec なら 4,000 まで伸ばせるが未対応）。
#: 取り込みが進んでからインデックス作成で落ちるより、設定した時点で
#: 弾いた方がよい。
MAX_INDEXABLE_DIMENSIONS = 2000

#: 既定の出力次元数。API の既定は 3,072 だが、それでは上の上限を超える。
DEFAULT_DIMENSIONS = 1536

#: 1 リクエストにまとめる件数。
DEFAULT_BATCH_SIZE = 100

#: 足切りの距離。**開発用ハッシュ実装の 0.85 とは別物**で、流用してはいけない。
#:
#: gemini-embedding-001 / 1,536 次元で、短い問い合わせ文 → 知識本文という
#: 実際の検索の形で測った分布（社内問い合わせ 6 件 × クエリ 14 本）:
#:
#:     正解ペア        min 0.224  p50 0.296  max 0.372
#:     不正解ペア      min 0.330  p50 0.465  max 0.525
#:     該当なしクエリ  min 0.401  p50 0.502  max 0.547
#:
#: 0.40 は「正解を 1 つも取りこぼさない上限」と「どの知識にも当てはまらない
#: クエリが 1 つも通らない下限」の間に収まる唯一の帯。0.45 まで緩めると
#: 該当なしのクエリが結果を返し始め、**新規登録の導線が消える**。0.35 まで
#: 締めると正解を取りこぼし始める。
#:
#: 不正解ペアが 0.40 で 4/55 通るのは許容している。正解より下位に並ぶだけで、
#: 順位は RRF が決めるため。守りたいのは「該当なしが空で返ること」の方。
DEFAULT_MAX_DISTANCE = 0.40

#: インライン画像として送れる上限。リクエスト全体で 20MB という制限があり、
#: base64 で 4/3 に膨らむぶんを見込んで余裕を取る。
MAX_INLINE_IMAGE_BYTES = 14 * 1024 * 1024

#: Gemini が受け付ける画像形式。
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
)

#: 文字起こしの指示。**説明ではなく文字の書き出しを求める**（markitdown の
#: 既定プロンプトは画像の説明文を作らせるもので、用途が違う）。
OCR_PROMPT = (
    "この画像に写っている文字をすべて書き出してください。"
    "見出し・箇条書き・表といったレイアウトの構造は Markdown で保ってください。"
    "説明・前置き・要約・感想は一切書かず、読み取れた文字だけを出力してください。"
    "文字が写っていない場合は、何も出力しないでください。"
)


def _normalize(values: list[float]) -> list[float]:
    """L2 正規化する。

    検索はコサイン距離で行うため、ベクトルは単位長であってほしい。
    Gemini は既定次元では正規化済みのベクトルを返すが、次元数を削ると
    正規化されないモデルがある。二重に正規化しても結果は変わらないので、
    モデルごとの差を気にせず済むよう常に掛ける。
    """
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return _unit_vector(len(values))
    return [v / norm for v in values]


def _unit_vector(dimensions: int) -> list[float]:
    """ゼロベクトルの代わりに使う決定的な単位ベクトル。

    ゼロベクトルはコサイン距離が定義できない。
    """
    vec = [0.0] * dimensions
    vec[0] = 1.0
    return vec


def _batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class _GeminiApi:
    """API キーと HTTP クライアントの面倒だけを見る土台。"""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = _API,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key が空です")
        self._api = api_base.rstrip("/")
        # キーは URL ではなくヘッダに載せる。クエリ文字列はアクセスログや
        # プロキシの記録に残りやすい
        self._client = client or httpx.Client(
            timeout=timeout, headers={"x-goog-api-key": api_key}
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api}{path}"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini への接続に失敗しました: {exc}") from exc

        if response.status_code == 429:
            raise ProviderError(
                "Gemini API のレート制限に達しました。時間をおいて再試行してください"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"Gemini API がエラーを返しました {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("Gemini API の応答を JSON として読めません") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class GeminiEmbeddingProvider(_GeminiApi):
    """Gemini の embedding。

    生成に使ったモデル名と次元数は呼び出し側が必ず保存する（異なるモデルの
    ベクトルは比較できないため）。model / dimensions を晒しているのはそのため。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_distance: float = DEFAULT_MAX_DISTANCE,
        api_base: str = _API,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not MIN_DIMENSIONS <= dimensions <= MAX_DIMENSIONS:
            raise ValueError(
                f"dimensions は {MIN_DIMENSIONS}〜{MAX_DIMENSIONS} の範囲で指定する"
                f"（指定値: {dimensions}）"
            )
        if dimensions > MAX_INDEXABLE_DIMENSIONS:
            raise ValueError(
                f"dimensions が {MAX_INDEXABLE_DIMENSIONS} を超えると pgvector の"
                f" HNSW インデックスを作れません（指定値: {dimensions}）。"
                f"{DEFAULT_DIMENSIONS} など {MAX_INDEXABLE_DIMENSIONS} 以下を指定してください"
            )
        if batch_size <= 0:
            raise ValueError("batch_size は 1 以上で指定する")
        super().__init__(
            api_key=api_key, api_base=api_base, client=client, timeout=timeout
        )
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
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
        return False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """入力と同じ順序・同じ長さで返す。

        空文字は API に投げず、決定的な単位ベクトルを返す。API が空入力を
        エラーにするうえ、無駄な課金にもなるため。
        """
        if not texts:
            return []

        targets = [(i, t) for i, t in enumerate(texts) if t.strip()]
        results: list[list[float]] = [
            _unit_vector(self._dimensions) for _ in range(len(texts))
        ]

        for batch in _batched(targets, self._batch_size):
            vectors = self._embed_batch([t for _, t in batch])
            for (index, _), vector in zip(batch, vectors):
                results[index] = vector
        return results

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "requests": [
                {
                    "model": f"models/{self._model}",
                    "content": {"parts": [{"text": text}]},
                    "output_dimensionality": self._dimensions,
                }
                for text in texts
            ]
        }
        body = self._post(f"/models/{self._model}:batchEmbedContents", payload)

        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ProviderError(
                f"Gemini API が期待した数のベクトルを返しませんでした"
                f"（要求 {len(texts)} 件）"
            )

        vectors: list[list[float]] = []
        for item in embeddings:
            values = (item or {}).get("values")
            if not isinstance(values, list) or not values:
                raise ProviderError("Gemini API の応答にベクトルが含まれていません")
            if len(values) != self._dimensions:
                # 次元数が食い違ったまま格納すると、検索時に初めて壊れる
                raise ProviderError(
                    f"Gemini API が {len(values)} 次元を返しました"
                    f"（要求は {self._dimensions} 次元）"
                )
            vectors.append(_normalize([float(v) for v in values]))
        return vectors


class GeminiLlmClient(_GeminiApi):
    """Gemini による画像の文字起こし。

    markitdown の `llm_client` には渡さない。markitdown は OpenAI 形式の
    `client.chat.completions.create` を直接呼ぶうえ、既定のプロンプトが
    「画像の説明」を求めるもので、文字起こしという用途に合わないため。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_LLM_MODEL,
        api_base: str = _API,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        prompt: str = OCR_PROMPT,
    ) -> None:
        super().__init__(
            api_key=api_key, api_base=api_base, client=client, timeout=timeout
        )
        self._model = model
        self._prompt = prompt

    @property
    def model(self) -> str:
        return self._model

    def extract_text_from_image(self, data: bytes, *, mime_type: str) -> str:
        mime = mime_type.split(";")[0].strip().lower()
        if mime not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ProviderError(
                f"Gemini が扱えない画像形式です: {mime_type}"
                f"（対応: {', '.join(sorted(SUPPORTED_IMAGE_MIME_TYPES))}）"
            )
        if len(data) > MAX_INLINE_IMAGE_BYTES:
            raise ProviderError(
                f"画像が大きすぎます（上限 {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)}MB）"
            )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": self._prompt},
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        },
                    ]
                }
            ]
        }
        body = self._post(f"/models/{self._model}:generateContent", payload)

        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise ProviderError(f"Gemini が画像の処理を拒否しました: {blocked}")

        candidates = body.get("candidates") or []
        if not candidates:
            # 文字が無い画像では候補が空になりうる。取り込み全体を止めるほどの
            # ことではないので、文字なしとして扱う
            return ""

        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        return "".join(p.get("text", "") for p in parts).strip()
