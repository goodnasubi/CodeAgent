"""プロバイダ共通の例外。"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """LLM / embedding プロバイダの呼び出しに失敗した。

    接続失敗・認証エラー・レート制限・不正なレスポンスをまとめて表す。
    取り込みの途中で起きても 1 件ぶんの失敗として扱えるよう、
    呼び出し側はこれだけを捕まえればよいようにしてある。
    """
