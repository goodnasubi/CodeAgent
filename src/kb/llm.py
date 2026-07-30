"""LLM クライアントの抽象。

LLM はテナントごとに選択できるため、呼び出し側は具体的なプロバイダを知らない。

**この抽象が持つのは、いまアプリが実際に使う操作だけ**（画像からの文字起こし）。
汎用のチャット API を先回りして定義しない。会話画面の応答はローカルで
組み立てており、LLM を呼んでいないため。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LlmClient(Protocol):
    @property
    def model(self) -> str:
        """モデル識別子。"""

    def extract_text_from_image(self, data: bytes, *, mime_type: str) -> str:
        """画像に写っている文字を書き出す。

        文字が無い画像では空文字を返す（例外にはしない）。
        """
