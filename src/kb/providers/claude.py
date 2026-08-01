"""Claude（Anthropic API）による画像の文字起こし。

**embedding は無い。** Anthropic は embedding の API を提供していないため、
このモジュールが持つのは LLM クライアントだけで、EmbeddingProvider の実装は
存在しない。検索は別プロバイダ（Gemini / OpenAI）の embedding が要る。
設定画面の embedding 側に claude が出てこないのはそのため。

**ここだけ公式 SDK（anthropic）を使う。** Gemini と OpenAI は httpx 直叩きで
揃えてあるが、こちらは SDK を採る:

- Anthropic 自身が SDK 利用を前提として案内しており、生 HTTP は明示的に
  必要な場合のみとされている。
- 429 / 5xx の指数バックオフ再試行と型付き例外が最初から入っている。
- 応答が thinking ブロックを含みうるなど、素の JSON を自前で解釈すると
  取りこぼす仕様がある（下記 _text_of を参照）。

依存が 1 つ増えるのは承知のうえで、上を優先している。
"""

from __future__ import annotations

import base64

import anthropic

from .base import ProviderError

#: 画像文字起こしの既定モデル。**画像 1 枚ごとに走る処理**なので、実機で
#: 文字起こしできることを確認したうえで最も安く速いものを既定にしている。
#: 実測（同一画像、3 モデル）: haiku-4-5 が 1.8 秒、sonnet-5 が 2.6 秒、
#: opus-5 が 4.7 秒。いずれも正しく読めた。
#:
#: 細かい文字の多い画像では上位モデルの方が有利なことがある。テナント設定で
#: モデル名を変えられる。
DEFAULT_LLM_MODEL = "claude-haiku-4-5"

#: 応答の上限。**thinking の有無をモデル任せにしているぶん、余裕を取る。**
#: 新しいモデルは熟考が既定で有効なことがあり、max_tokens は thinking と
#: 本文の合計に掛かる。ここを切り詰めると本文が途中で切れる。
DEFAULT_MAX_TOKENS = 8192

#: インライン画像として送れる上限。base64 で 4/3 に膨らむぶんを見込んだ
#: 自前の目安であって、API の公表値そのものではない。
MAX_INLINE_IMAGE_BYTES = 14 * 1024 * 1024

#: Claude が受け付ける画像形式。
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


def _text_of(content) -> str:
    """応答から本文だけを取り出す。

    **content[0] を決め打ちで読まない。** 熟考するモデルでは先頭が thinking
    ブロックになり、text は後ろに来る（実測で sonnet-5 がそうなった）。
    どのブロックが来るかはモデルと状況で変わるので、type で選ぶ。
    """
    return "".join(b.text for b in content if b.type == "text").strip()


class ClaudeLlmClient:
    """Claude による画像の文字起こし。

    markitdown の `llm_client` には渡さない。OpenAI 形式の呼び出しを前提に
    しているうえ、既定プロンプトが「画像の説明」を求めるもので用途が違う
    （Gemini / OpenAI 側と同じ理由）。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = 60.0,
        prompt: str = OCR_PROMPT,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key が空です")
        self._model = model
        self._max_tokens = max_tokens
        self._prompt = prompt
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout)

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def extract_text_from_image(self, data: bytes, *, mime_type: str) -> str:
        mime = mime_type.split(";")[0].strip().lower()
        if mime not in SUPPORTED_IMAGE_MIME_TYPES:
            raise ProviderError(
                f"Claude が扱えない画像形式です: {mime_type}"
                f"（対応: {', '.join(sorted(SUPPORTED_IMAGE_MIME_TYPES))}）"
            )
        if len(data) > MAX_INLINE_IMAGE_BYTES:
            raise ProviderError(
                f"画像が大きすぎます（上限 {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)}MB）"
            )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime,
                                    "data": base64.standard_b64encode(data).decode(
                                        "ascii"
                                    ),
                                },
                            },
                            {"type": "text", "text": self._prompt},
                        ],
                    }
                ],
            )
        # 具体的なものから順に捕まえる。まとめて 1 つで受けると、再試行できる
        # 失敗（429 / 5xx / 通信断）とできない失敗（鍵違い・モデル名違い）の
        # 区別が消える
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Claude API の認証に失敗しました（API キーを確認）") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(
                f"Claude API がモデルを見つけられません: {self._model}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(
                "Claude API のレート制限に達しました。時間をおいて再試行してください"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Claude API がエラーを返しました {exc.status_code}: {str(exc)[:300]}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Claude への接続に失敗しました: {exc}") from exc

        # **content を読む前に stop_reason を見る。** 安全側の判断で拒否された
        # 場合、HTTP は 200 のまま content が空か途中までになる
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) if detail else None
            raise ProviderError(
                "Claude が画像の処理を拒否しました"
                + (f": {category}" if category else "")
            )

        return _text_of(response.content)
