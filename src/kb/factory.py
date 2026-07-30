"""テナント設定から、実際に使う部品を組み立てる。"""

from __future__ import annotations

import os
import smtplib
from typing import Sequence
from uuid import UUID

import psycopg

from .backends.base import KnowledgeBase
from .backends.github import GitHubKnowledgeBase
from .backends.gitlab import GitLabKnowledgeBase
from .backends.redmine import RedmineKnowledgeBase
from .backends.relation import RelationKnowledgeBase
from .db.notifications import NotificationRepository
from .db.repository import ChunkRepository
from .db.sync import SyncStateRepository
from .embeddings import EmbeddingProvider, HashingEmbeddingProvider
from .ingest import IngestPipeline
from .llm import LlmClient
from .notifications import (
    EmailNotifier,
    InAppNotifier,
    NotificationDispatcher,
    Notifier,
    SlackNotifier,
)
from .providers import gemini
from .providers.gemini import GeminiEmbeddingProvider, GeminiLlmClient
from .sync import SyncRunner
from .tenants import (
    KIND_EMBEDDING,
    KIND_LLM,
    KbConnection,
    ModelSettings,
    TenantRepository,
    load_cipher,
)

KB_TYPES = ("github", "gitlab", "redmine", "relation")

SMTP_HOST_ENV = "KB_SMTP_HOST"
SMTP_PORT_ENV = "KB_SMTP_PORT"
SMTP_SENDER_ENV = "KB_SMTP_SENDER"


class UnknownBackend(ValueError):
    pass


class BackendNotConfigured(RuntimeError):
    """テナントに知識ベースが設定されていない。"""


class ProviderNotConfigured(RuntimeError):
    """プロバイダを選んでいるのに API キーが無い。"""


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


def build_embedder(
    settings: ModelSettings, *, api_key: str | None = None
) -> EmbeddingProvider:
    """embedding プロバイダを作る。

    Anthropic は embedding の API を提供していないため、**LLM の選択肢と
    embedding の選択肢は一致しない**。`claude` は LLM 側にしか現れない。
    """
    if settings.embedding_provider == "hashing":
        return HashingEmbeddingProvider(
            dimensions=settings.embedding_dim, model=settings.embedding_model
        )

    if settings.embedding_provider == "gemini":
        if not api_key:
            raise ProviderNotConfigured(
                "Gemini の API キーが設定されていません"
            )
        return GeminiEmbeddingProvider(
            api_key=api_key,
            model=settings.embedding_model or gemini.DEFAULT_EMBEDDING_MODEL,
            dimensions=settings.embedding_dim,
        )

    raise UnknownBackend(
        f"embedding プロバイダ '{settings.embedding_provider}' は未対応です"
    )


def build_llm(settings: ModelSettings, *, api_key: str | None = None) -> LlmClient | None:
    """LLM クライアントを作る。

    **未設定なら None を返す**（例外にしない）。LLM を使うのは画像の
    文字起こしだけで、設定していないテナントでも他の機能は動くため。
    """
    if settings.llm_provider == "gemini" and api_key:
        return GeminiLlmClient(
            api_key=api_key, model=settings.llm_model or gemini.DEFAULT_LLM_MODEL
        )
    return None


def embedder_for_tenant(
    *, tenants: TenantRepository, tenant_id: UUID
) -> EmbeddingProvider:
    return build_embedder(
        tenants.get_model_settings(tenant_id),
        api_key=tenants.get_model_api_key(tenant_id, KIND_EMBEDDING),
    )


def llm_for_tenant(*, tenants: TenantRepository, tenant_id: UUID) -> LlmClient | None:
    return build_llm(
        tenants.get_model_settings(tenant_id),
        api_key=tenants.get_model_api_key(tenant_id, KIND_LLM),
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


def build_shared_notifiers() -> list[Notifier]:
    """DB 接続に依存しない通知チャネル。

    常駐プロセスでは作り直さずに使い回せる（Slack は HTTP クライアントを
    抱えるため、周回ごとに作ると接続が積み上がる）。

    メールは SMTP の設定が要るので、環境変数が無ければ登録しない。未登録の
    チャネルに対する通知規則は「未対応のチャネル」として `DispatchResult.failed`
    に載る（黙って消えるより、設定漏れとして見えた方がよい）。
    """
    notifiers: list[Notifier] = [SlackNotifier()]

    host = os.environ.get(SMTP_HOST_ENV)
    if host:
        port = int(os.environ.get(SMTP_PORT_ENV, "25"))
        notifiers.append(
            EmailNotifier(
                sender=os.environ.get(SMTP_SENDER_ENV, "kb@localhost"),
                smtp_factory=lambda: smtplib.SMTP(host, port),
            )
        )
    return notifiers


def build_notifiers(
    repository: NotificationRepository, *, shared: Sequence[Notifier] | None = None
) -> list[Notifier]:
    """使える通知チャネルを揃える。アプリ内通知だけは DB 接続を要する。"""
    stateless = list(shared) if shared is not None else build_shared_notifiers()
    return [InAppNotifier(repository), *stateless]


def build_sync_runner(
    *,
    conn: psycopg.Connection,
    tenant_id: UUID,
    tenants: TenantRepository | None = None,
    notifiers: Sequence[Notifier] | None = None,
) -> SyncRunner:
    """1 テナントぶんの取り込み一式を組み立てる。

    常駐スケジューラと開発者画面の手動実行で同じものを使う。片方だけ通知が
    飛ばない、といった食い違いが起きないようにするため。
    """
    repo = tenants or TenantRepository(conn, cipher=load_cipher())
    notifications = NotificationRepository(conn)
    return SyncRunner(
        pipeline=IngestPipeline(
            repository=ChunkRepository(conn),
            embedder=embedder_for_tenant(tenants=repo, tenant_id=tenant_id),
        ),
        backend=backend_for_tenant(tenants=repo, tenant_id=tenant_id),
        state=SyncStateRepository(conn),
        dispatcher=NotificationDispatcher(
            repository=notifications,
            notifiers=list(notifiers) if notifiers is not None
            else build_notifiers(notifications),
        ),
    )
