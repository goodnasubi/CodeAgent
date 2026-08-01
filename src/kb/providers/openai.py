"""OpenAI による embedding と画像文字起こし。

`api.openai.com` を API キー 1 本で叩く。SDK（openai）は使わず httpx で
直接呼ぶ。バックエンド 4 種および Gemini プロバイダと同じ流儀で、使うのが
2 エンドポイントだけであること、依存を増やさないことによる。

Azure OpenAI に移すときは api_base と認証ヘッダだけを差し替えれば済むよう、
他は分けてある。
"""

from __future__ import annotations

import base64
import math
from typing import Any, Iterator, Sequence

import httpx

from .base import ProviderError

_API = "https://api.openai.com/v1"

#: embedding の既定モデル。text-embedding-3-large は既定 3,072 次元で
#: pgvector のインデックス上限を超えるため、small を既定にしている。
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

#: 画像文字起こしの既定モデル。
#:
#: ⚠️ **未検証。** /v1/models の一覧に存在することは確認したが、実際に
#: 文字起こしさせる検証ができていない（検証時点でアカウントの残高が尽きて
#: おり、全モデルが 429 insufficient_quota を返した）。一覧にあることは
#: 使えることを意味しない——Gemini 側で、一覧に出るのに新しいキーでは 404 に
#: なるモデルを実際に踏んでいる。
#:
#: 残高を入れたら tests/test_providers_openai_live.py を流し、通らなければ
#: 他の候補（gpt-5.2 / gpt-4.1-mini / gpt-4o-mini など）に差し替えること。
DEFAULT_LLM_MODEL = "gpt-5-mini"

#: text-embedding-3-* が受け付ける出力次元数の下限。
MIN_DIMENSIONS = 1

#: pgvector の HNSW インデックスが張れる上限。**これを超える次元数は
#: 検索インデックスを作れない**（halfvec なら 4,000 まで伸ばせるが未対応）。
#: 取り込みが進んでからインデックス作成で落ちるより、設定した時点で
#: 弾いた方がよい。
MAX_INDEXABLE_DIMENSIONS = 2000

#: 既定の出力次元数。text-embedding-3-small の既定と同じ。
DEFAULT_DIMENSIONS = 1536

#: 足切りの距離。
#:
#: ⚠️ **これは実測値ではない。Gemini の値を仮に置いているだけ。**
#: 足切りはプロバイダごとに違う値であり、流用してはいけない——というのが
#: そもそもこの定数が provider ごとにある理由なので、この状態は本来まずい。
#: 検証時点でアカウントの残高が尽きており、分布を測れなかった。
#:
#: 残高を入れたら、Gemini と同じ方法で測って差し替えること:
#: 短いクエリ文 → 知識本文の距離を、正解ペア・不正解ペア・どの知識にも
#: 当てはまらないクエリの 3 群で取り、「正解を取りこぼさない上限」と
#: 「該当なしが 1 つも通らない下限」の間に置く。
#: tests/test_providers_openai_live.py の
#: test_max_distance_separates_relevant_from_irrelevant が最低限の番人。
DEFAULT_MAX_DISTANCE = 0.40

#: 1 リクエストにまとめる件数。
DEFAULT_BATCH_SIZE = 100

#: インライン画像として送れる上限。base64 で 4/3 に膨らむぶんを見込む。
MAX_INLINE_IMAGE_BYTES = 14 * 1024 * 1024

#: 受け付ける画像形式。
SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: 文字起こしの指示。**説明ではなく文字の書き出しを求める。**
OCR_PROMPT = (
    "この画像に写っている文字をすべて書き出してください。"
    "見出し・箇条書き・表といったレイアウトの構造は Markdown で保ってください。"
    "説明・前置き・要約・感想は一切書かず、読み取れた文字だけを出力してください。"
    "文字が写っていない場合は、何も出力しないでください。"
)


def _normalize(values: list[float]) -> list[float]:
    """L2 正規化する。

    検索はコサイン距離で行うため、ベクトルは単位長であってほしい。
    OpenAI は既定次元では正規化済みで返すが、`dimensions` で次元を削ると
    正規化されない。二重に正規化しても結果は変わらないので常に掛ける。
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


class _OpenAiApi:
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
        self._client = client or httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api}{path}"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI への接続に失敗しました: {exc}") from exc

        if response.status_code == 429:
            # **429 は 2 つの意味を持つ。** 本当のレート制限と、残高不足
            # （insufficient_quota）。後者に「時間をおいて再試行」と案内すると
            # 永久に直らないものを待たせることになるので、必ず区別する
            if self._error_type(response) == "insufficient_quota":
                raise ProviderError(
                    "OpenAI API の残高が不足しています。"
                    "クレジットを追加してください（時間をおいても回復しません）: "
                    f"{self._error_message(response)}"
                )
            raise ProviderError(
                "OpenAI API のレート制限に達しました。時間をおいて再試行してください"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"OpenAI API がエラーを返しました {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("OpenAI API の応答を JSON として読めません") from exc

    @staticmethod
    def _error_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {}

    @classmethod
    def _error_type(cls, response: httpx.Response) -> str:
        return str(cls._error_body(response).get("type") or "")

    @classmethod
    def _error_message(cls, response: httpx.Response) -> str:
        return str(cls._error_body(response).get("message") or response.text[:200])

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class OpenAiEmbeddingProvider(_OpenAiApi):
    """OpenAI の embedding。

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
        if dimensions < MIN_DIMENSIONS:
            raise ValueError(f"dimensions は 1 以上で指定する（指定値: {dimensions}）")
        if dimensions > MAX_INDEXABLE_DIMENSIONS:
            raise ValueError(
                f"dimensions が {MAX_INDEXABLE_DIMENSIONS} を超えると pgvector の"
                f" HNSW インデックスを作れません（指定値: {dimensions}）。"
                f"{DEFAULT_DIMENSIONS} など {MAX_INDEXABLE_DIMENSIONS} 以下を"
                "指定してください"
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
        body = self._post(
            "/embeddings",
            {
                "model": self._model,
                "input": texts,
                "dimensions": self._dimensions,
                "encoding_format": "float",
            },
        )

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ProviderError(
                f"OpenAI API が期待した数のベクトルを返しませんでした"
                f"（要求 {len(texts)} 件）"
            )

        # **応答の順序を信用しない。** 各要素の index が入力の位置を示すので、
        # それに従って並べ直す。順序がずれると別の知識のベクトルを保存する
        slots: list[list[float] | None] = [None] * len(texts)
        for item in data:
            index = (item or {}).get("index")
            if not isinstance(index, int) or not 0 <= index < len(texts):
                raise ProviderError("OpenAI API の応答に不正な index が含まれています")
            values = (item or {}).get("embedding")
            if not isinstance(values, list) or not values:
                raise ProviderError("OpenAI API の応答にベクトルが含まれていません")
            if len(values) != self._dimensions:
                # 次元数が食い違ったまま格納すると、検索時に初めて壊れる
                raise ProviderError(
                    f"OpenAI API が {len(values)} 次元を返しました"
                    f"（要求は {self._dimensions} 次元）"
                )
            slots[index] = _normalize([float(v) for v in values])

        if any(v is None for v in slots):
            raise ProviderError("OpenAI API の応答に欠けている入力があります")
        return [v for v in slots if v is not None]


class OpenAiLlmClient(_OpenAiApi):
    """OpenAI による画像の文字起こし。

    markitdown の `llm_client` には渡さない。既定のプロンプトが「画像の説明」を
    求めるもので、文字起こしという用途に合わないため（Gemini 側と同じ理由）。
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
                f"OpenAI が扱えない画像形式です: {mime_type}"
                f"（対応: {', '.join(sorted(SUPPORTED_IMAGE_MIME_TYPES))}）"
            )
        if len(data) > MAX_INLINE_IMAGE_BYTES:
            raise ProviderError(
                f"画像が大きすぎます（上限 {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)}MB）"
            )

        encoded = base64.b64encode(data).decode("ascii")
        body = self._post(
            "/chat/completions",
            {
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self._prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            },
                        ],
                    }
                ],
            },
        )

        choices = body.get("choices") or []
        if not choices:
            # 文字が無い画像では空になりうる。取り込み全体を止めるほどの
            # ことではないので、文字なしとして扱う
            return ""

        message = (choices[0] or {}).get("message") or {}
        if message.get("refusal"):
            raise ProviderError(
                f"OpenAI が画像の処理を拒否しました: {message['refusal']}"
            )
        return (message.get("content") or "").strip()
