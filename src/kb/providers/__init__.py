"""LLM / embedding の実プロバイダ。

バックエンドと同じく、プロバイダごとに 1 モジュール。共通の抽象は
`kb.embeddings`（EmbeddingProvider）と `kb.llm`（LlmClient）にある。
"""

from .base import ProviderError

__all__ = ["ProviderError"]
