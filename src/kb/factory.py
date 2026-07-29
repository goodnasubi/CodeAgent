"""テナント設定から、実際に使う部品を組み立てる。"""

from __future__ import annotations

from uuid import UUID

from .backends.base import KnowledgeBase
from .backends.github import GitHubKnowledgeBase
from .backends.gitlab import GitLabKnowledgeBase
from .backends.redmine import RedmineKnowledgeBase
from .backends.relation import RelationKnowledgeBase
from .embeddings import EmbeddingProvider, HashingEmbeddingProvider
from .tenants import KbConnection, ModelSettings, TenantRepository

KB_TYPES = ("github", "gitlab", "redmine", "relation")


class UnknownBackend(ValueError):
    pass


class BackendNotConfigured(RuntimeError):
    """テナントに知識ベースが設定されていない。"""


def build_backend(
    *, connection: KbConnection, token: str
) -> KnowledgeBase:
    """接続設定からアダプタを作る。"""
    extra = connection.extra or {}

    if connection.kb_type == "github":
        kwargs = {"token": token, "repository": connection.project}
        if connection.base_url:
            kwargs["api_base"] = connection.base_url
        return GitHubKnowledgeBase(**kwargs)

    if connection.kb_type == "gitlab":
        kwargs = {"token": token, "project": connection.project}
        if connection.base_url:
            kwargs["api_base"] = connection.base_url
        return GitLabKnowledgeBase(**kwargs)

    if connection.kb_type == "redmine":
        if not connection.base_url:
            raise UnknownBackend("Redmine は base_url が必須です")
        return RedmineKnowledgeBase(
            api_key=token,
            base_url=connection.base_url,
            project=connection.project,
            label_field=extra.get("label_field", "Labels"),
        )

    if connection.kb_type == "relation":
        if not extra.get("message_box_id"):
            raise UnknownBackend("Re:lation は message_box_id が必須です")
        return RelationKnowledgeBase(
            access_token=token,
            subdomain=connection.project,
            message_box_id=extra["message_box_id"],
        )

    raise UnknownBackend(f"未対応の知識ベースです: {connection.kb_type}")


def build_embedder(settings: ModelSettings) -> EmbeddingProvider:
    """embedding プロバイダを作る。

    実プロバイダ（OpenAI / Gemini / Claude）は API キーが要るため未実装。
    それまでは開発用のハッシュ実装で動かす。
    """
    if settings.embedding_provider == "hashing":
        return HashingEmbeddingProvider(
            dimensions=settings.embedding_dim, model=settings.embedding_model
        )
    raise UnknownBackend(
        f"embedding プロバイダ '{settings.embedding_provider}' は未実装です"
        "（API キーが必要なため）"
    )


def backend_for_tenant(
    *, tenants: TenantRepository, tenant_id: UUID
) -> KnowledgeBase:
    connection = tenants.get_kb_connection(tenant_id)
    if connection is None:
        raise BackendNotConfigured("このテナントには知識ベースが設定されていません")
    token = tenants.get_kb_token(tenant_id)
    if token is None:
        raise BackendNotConfigured("接続トークンが保存されていません")
    return build_backend(connection=connection, token=token)
