"""HTTP API。React の画面から叩く。

認証は持たない（管理者が払い出した識別子で動く）。**社内クローズド運用が
前提**で、インターネットに露出させる場合は認証基盤が必須になる。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .backends.base import KnowledgeBaseError
from .db.conversations import ROLE_ASSISTANT, ROLE_USER, ConversationRepository
from .db.notifications import NotificationRepository
from .db.repository import ChunkRepository
from .db.sync import SyncInProgress, SyncStateRepository, tenant_sync_lock
from .documents import (
    ConversionError,
    ConvertedDocument,
    DocumentConverter,
    UnsupportedSource,
)
from .factory import (
    KB_TYPES,
    BackendNotConfigured,
    ProviderNotConfigured,
    backend_for_tenant,
    build_sync_runner,
    embedder_for_tenant,
    llm_for_tenant,
)
from .ingest import IngestPipeline
from .search import HybridSearch
from .sync import MAX_ITEM_ATTEMPTS, interval_from_env
from .tenants import (
    KIND_EMBEDDING,
    KIND_LLM,
    KbConnection,
    ModelSettings,
    TenantRepository,
    load_cipher,
)

DSN_ENV = "KB_DSN"

#: 取り出した本文の上限。GitHub の Issue 本文が 65,536 文字までなので、
#: 利用者が書き足す分を残してそれより手前で切る
MAX_EXTRACTED_CHARS = 60_000


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} が未設定です")
    return dsn


@contextmanager
def _connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        yield conn


def get_conn() -> Iterator[psycopg.Connection]:
    with _connect() as conn:
        yield conn


# ------------------------------------------------------------------ schemas


class TenantIn(BaseModel):
    name: str


class TenantOut(BaseModel):
    id: UUID
    name: str


class AccountIn(BaseModel):
    display_name: str = ""


class AccountOut(BaseModel):
    id: UUID
    tenant_id: UUID
    display_name: str


class KbConnectionIn(BaseModel):
    kb_type: str
    project: str
    token: str
    base_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class KbConnectionOut(BaseModel):
    """トークンは決して返さない。"""

    kb_type: str
    project: str
    base_url: str | None
    extra: dict[str, Any]
    supports_keyword_search: bool | None = None
    supports_relations: bool | None = None


class ModelSettingsIn(BaseModel):
    llm_provider: str = "claude"
    llm_model: str = ""
    embedding_provider: str = "hashing"
    embedding_model: str = "hashing-dev"
    embedding_dim: int = 768
    # 省略時は保存済みの鍵をそのまま残す。プロバイダ名だけ直したいときに
    # 空文字で上書きして消してしまわないよう、None と "" を区別する。
    llm_api_key: str | None = None
    embedding_api_key: str | None = None


class ModelSettingsOut(BaseModel):
    """API キーは決して返さない。設定済みかどうかだけ伝える。"""

    llm_provider: str
    llm_model: str
    embedding_provider: str
    embedding_model: str
    embedding_dim: int
    has_llm_api_key: bool = False
    has_embedding_api_key: bool = False


class SearchIn(BaseModel):
    query: str
    limit: int = 10


class SearchHitOut(BaseModel):
    kb_issue_id: str
    title: str
    url: str | None
    score: float
    sources: list[str]
    labels: list[str]


class SearchOut(BaseModel):
    results: list[SearchHitOut]
    used: list[str]
    skipped: dict[str, str]
    development_embedding: bool = False
    """開発用の embedding で検索した。画面に出して利用者に知らせるためのもの。

    テナントの既定がハッシュ実装なので、鍵を設定しないまま使い始めても
    検索は動き、それらしい件数まで返る。**結果が意味を持たないことは
    画面からしか分からない。**
    """


class UrlIn(BaseModel):
    url: str


class ExtractedOut(BaseModel):
    """取り込んだ素材を、そのまま検索欄や登録フォームに流し込める形で返す。"""

    source_name: str
    text: str
    truncated: bool = False


class KnowledgeIn(BaseModel):
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)


class AppendIn(BaseModel):
    text: str


class KnowledgeOut(BaseModel):
    id: str
    title: str
    url: str
    labels: list[str]


class RuleIn(BaseModel):
    label: str
    channel: str
    destination: str = ""


class NotificationOut(BaseModel):
    id: int
    kb_issue_id: str
    label: str
    title: str
    url: str | None


class MarkReadIn(BaseModel):
    ids: list[int]


class ConversationOut(BaseModel):
    id: UUID
    title: str


class MessageIn(BaseModel):
    role: str
    content: str


class MessageOut(BaseModel):
    id: int
    role: str
    content: str


# --------------------------------------------------------------------- app


def create_app() -> FastAPI:
    app = FastAPI(title="知識ベース検索・登録システム", version="0.1.0")

    # 設定の誤りは起動時に出す。壊れた値をリクエストのたびに 500 にするより、
    # 立ち上がらない方が気づける（KB_SECRET_KEY と同じ考え方）
    sync_interval = interval_from_env()

    # 社内クローズド運用が前提。開発時にフロントを別ポートで動かすため許可する
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("KB_CORS_ORIGINS", "*").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def tenants_repo(conn: psycopg.Connection = Depends(get_conn)) -> TenantRepository:
        return TenantRepository(conn, cipher=load_cipher())

    @app.exception_handler(ProviderNotConfigured)
    def _provider_not_configured(request: Request, exc: ProviderNotConfigured):
        # 設定漏れであってサーバーの障害ではないので 500 にしない
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # ------------------------------------------------------------ 開発者向け

    @app.post("/api/admin/tenants", response_model=TenantOut, tags=["developer"])
    def create_tenant(body: TenantIn, repo: TenantRepository = Depends(tenants_repo)):
        """テナントを払い出す。利用者は自分で作れない。"""
        tenant = repo.create_tenant(name=body.name)
        return TenantOut(id=tenant.id, name=tenant.name)

    @app.get("/api/admin/tenants", response_model=list[TenantOut], tags=["developer"])
    def list_tenants(repo: TenantRepository = Depends(tenants_repo)):
        return [TenantOut(id=t.id, name=t.name) for t in repo.list_tenants()]

    @app.post(
        "/api/admin/tenants/{tenant_id}/accounts",
        response_model=AccountOut,
        tags=["developer"],
    )
    def create_account(
        tenant_id: UUID, body: AccountIn, repo: TenantRepository = Depends(tenants_repo)
    ):
        if repo.get_tenant(tenant_id) is None:
            raise HTTPException(404, "テナントが見つかりません")
        account = repo.create_account(
            tenant_id=tenant_id, display_name=body.display_name
        )
        return AccountOut(**account.__dict__)

    @app.post("/api/admin/tenants/{tenant_id}/sync", tags=["developer"])
    def run_sync(tenant_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
        """取り込みを手動で走らせる。定期実行は `python -m kb.scheduler` が担う。

        常駐スケジューラと同じ組み立て・同じロックを使う。両者が同時に同じ
        テナントを取り込むとラベルの差分検知が二重に走り、通知が二度飛ぶ。
        """
        try:
            with tenant_sync_lock(conn, tenant_id=tenant_id):
                runner = build_sync_runner(conn=conn, tenant_id=tenant_id)
                report = runner.sync(tenant_id=tenant_id)
        except SyncInProgress as exc:
            raise HTTPException(409, str(exc)) from exc
        except BackendNotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

        return {
            "ingested": report.ingested,
            "notified": report.notified,
            "failed": [{"id": i, "error": e} for i, e in report.failed],
            "quarantined": [{"id": i, "error": e} for i, e in report.quarantined],
            "aborted": report.aborted,
        }

    @app.get("/api/admin/tenants/{tenant_id}/sync", tags=["developer"])
    def sync_status(tenant_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
        repo = SyncStateRepository(conn)
        state = repo.get(tenant_id=tenant_id)
        # **見送った知識は必ず返す。** これを出さないと、取り込みが正常に
        # 進んでいるのに一部の知識だけ永久に入らない状態に誰も気づけない
        failures = [
            {
                "kb_issue_id": f.kb_issue_id,
                "attempts": f.attempts,
                "last_error": f.last_error,
                "quarantined": f.attempts >= MAX_ITEM_ATTEMPTS,
                "first_failed_at": f.first_failed_at,
                "last_failed_at": f.last_failed_at,
            }
            for f in repo.failures(tenant_id=tenant_id)
        ]
        # 画面側が「何分おきに動くはずか」を示せるように間隔も返す
        return {
            "last_synced_at": state.last_synced_at if state else None,
            "last_run_at": state.last_run_at if state else None,
            "last_error": state.last_error if state else None,
            "interval_seconds": sync_interval,
            "failures": failures,
        }

    # ------------------------------------------------------------ 管理設定

    @app.get("/api/tenants/{tenant_id}/kb", response_model=KbConnectionOut | None,
             tags=["settings"])
    def get_kb(tenant_id: UUID, repo: TenantRepository = Depends(tenants_repo)):
        connection = repo.get_kb_connection(tenant_id)
        if connection is None:
            return None
        out = KbConnectionOut(
            kb_type=connection.kb_type,
            project=connection.project,
            base_url=connection.base_url,
            extra=connection.extra or {},
        )
        try:
            backend = backend_for_tenant(tenants=repo, tenant_id=tenant_id)
        except (BackendNotConfigured, ValueError):
            return out
        out.supports_keyword_search = backend.supports_keyword_search
        out.supports_relations = backend.supports_relations
        return out

    @app.put("/api/tenants/{tenant_id}/kb", tags=["settings"])
    def set_kb(
        tenant_id: UUID, body: KbConnectionIn, repo: TenantRepository = Depends(tenants_repo)
    ):
        if body.kb_type not in KB_TYPES:
            raise HTTPException(400, f"未対応の知識ベースです: {body.kb_type}")
        repo.set_kb_connection(
            tenant_id=tenant_id,
            connection=KbConnection(
                kb_type=body.kb_type,
                project=body.project,
                base_url=body.base_url,
                extra=body.extra,
            ),
            token=body.token,
        )
        return {"ok": True}

    @app.get("/api/tenants/{tenant_id}/models", response_model=ModelSettingsOut,
             tags=["settings"])
    def get_models(tenant_id: UUID, repo: TenantRepository = Depends(tenants_repo)):
        return ModelSettingsOut(
            **repo.get_model_settings(tenant_id).__dict__,
            has_llm_api_key=repo.get_model_api_key(tenant_id, KIND_LLM) is not None,
            has_embedding_api_key=(
                repo.get_model_api_key(tenant_id, KIND_EMBEDDING) is not None
            ),
        )

    @app.put("/api/tenants/{tenant_id}/models", tags=["settings"])
    def set_models(
        tenant_id: UUID,
        body: ModelSettingsIn,
        repo: TenantRepository = Depends(tenants_repo),
    ):
        settings = body.model_dump(exclude={"llm_api_key", "embedding_api_key"})
        repo.set_model_settings(tenant_id=tenant_id, settings=ModelSettings(**settings))
        for kind, key in (
            (KIND_LLM, body.llm_api_key),
            (KIND_EMBEDDING, body.embedding_api_key),
        ):
            if key is not None:
                repo.set_model_api_key(tenant_id=tenant_id, kind=kind, api_key=key)
        return {"ok": True}

    @app.get("/api/tenants/{tenant_id}/accounts", response_model=list[AccountOut],
             tags=["account"])
    def list_accounts(tenant_id: UUID, repo: TenantRepository = Depends(tenants_repo)):
        return [AccountOut(**a.__dict__) for a in repo.list_accounts(tenant_id)]

    # ---------------------------------------------------------- 素材の取り込み

    @app.post("/api/tenants/{tenant_id}/extract/file", response_model=ExtractedOut,
              tags=["documents"])
    async def extract_file(
        tenant_id: UUID,
        file: UploadFile = File(...),
        conn: psycopg.Connection = Depends(get_conn),
    ):
        """アップロードされたファイルを本文テキストにして返す。

        ここでは KB にも DB にも書かない。取り出した本文で**まず検索し**、
        見つからなければ登録する、という流れのための素材を返すだけ。
        """
        data = await file.read()
        return _extract(
            tenant_id,
            conn,
            lambda c: c.convert_bytes(data, filename=file.filename or "upload"),
        )

    @app.post("/api/tenants/{tenant_id}/extract/url", response_model=ExtractedOut,
              tags=["documents"])
    def extract_url(
        tenant_id: UUID, body: UrlIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        return _extract(tenant_id, conn, lambda c: c.convert_url(body.url))

    def _extract(tenant_id: UUID, conn: psycopg.Connection, run: Any) -> ExtractedOut:
        # 画像の文字起こしはテナントが選んだ LLM に委譲する。未設定なら
        # None が返り、画像は文字なしとして扱われる（他の形式は影響を受けない）。
        repo = TenantRepository(conn, cipher=load_cipher())
        converter = DocumentConverter(
            llm=llm_for_tenant(tenants=repo, tenant_id=tenant_id)
        )
        try:
            converted: ConvertedDocument = run(converter)
        except UnsupportedSource as exc:
            raise HTTPException(400, str(exc)) from exc
        except ConversionError as exc:
            raise HTTPException(422, str(exc)) from exc

        text = converted.text
        # KB 側の本文長に収める。GitHub の Issue 本文は 65,536 文字までで、
        # 超えると登録そのものが弾かれる
        truncated = len(text) > MAX_EXTRACTED_CHARS
        if truncated:
            text = text[:MAX_EXTRACTED_CHARS]
        return ExtractedOut(
            source_name=converted.source_name, text=text, truncated=truncated
        )

    # -------------------------------------------------------------- 検索

    @app.post("/api/tenants/{tenant_id}/search", response_model=SearchOut, tags=["search"])
    def search(
        tenant_id: UUID, body: SearchIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        repo = TenantRepository(conn, cipher=load_cipher())
        try:
            backend = backend_for_tenant(tenants=repo, tenant_id=tenant_id)
        except BackendNotConfigured:
            backend = None  # KB 未設定でも類似度検索は動く

        response = HybridSearch(
            repository=ChunkRepository(conn),
            embedder=embedder_for_tenant(tenants=repo, tenant_id=tenant_id),
            backend=backend,
        ).search(tenant_id=tenant_id, query=body.query, limit=body.limit)

        return SearchOut(
            results=[
                SearchHitOut(
                    kb_issue_id=r.kb_issue_id,
                    title=r.title,
                    url=r.url,
                    score=r.score,
                    sources=list(r.sources),
                    labels=list(r.labels),
                )
                for r in response.results
            ],
            used=list(response.diagnostics.used),
            skipped=dict(response.diagnostics.skipped),
            development_embedding=response.diagnostics.development_embedding,
        )

    # ------------------------------------------------------------- 知識

    @app.post("/api/tenants/{tenant_id}/knowledge", response_model=KnowledgeOut,
              tags=["knowledge"])
    def register(
        tenant_id: UUID, body: KnowledgeIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        """知識を新規登録する。**KB 側に作ってから**こちらに取り込む。"""
        repo = TenantRepository(conn, cipher=load_cipher())
        try:
            backend = backend_for_tenant(tenants=repo, tenant_id=tenant_id)
        except BackendNotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            created = backend.create(
                title=body.title, body=body.body, labels=body.labels
            )
        except KnowledgeBaseError as exc:
            # KB が正なので、そこに書けなければ登録は成立しない
            raise HTTPException(502, f"知識ベースに書き込めませんでした: {exc}") from exc

        IngestPipeline(
            repository=ChunkRepository(conn),
            embedder=embedder_for_tenant(tenants=repo, tenant_id=tenant_id),
        ).ingest_knowledge(tenant_id=tenant_id, knowledge=created)

        return KnowledgeOut(
            id=created.id,
            title=created.title,
            url=created.url,
            labels=list(created.labels),
        )

    @app.post("/api/tenants/{tenant_id}/knowledge/{knowledge_id}/append",
              tags=["knowledge"])
    def append(
        tenant_id: UUID,
        knowledge_id: str,
        body: AppendIn,
        conn: psycopg.Connection = Depends(get_conn),
    ):
        repo = TenantRepository(conn, cipher=load_cipher())
        try:
            backend = backend_for_tenant(tenants=repo, tenant_id=tenant_id)
        except BackendNotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            backend.append(knowledge_id, body.text)
            refreshed = backend.get(knowledge_id)
        except KnowledgeBaseError as exc:
            raise HTTPException(502, str(exc)) from exc

        IngestPipeline(
            repository=ChunkRepository(conn),
            embedder=embedder_for_tenant(tenants=repo, tenant_id=tenant_id),
        ).ingest_knowledge(tenant_id=tenant_id, knowledge=refreshed)
        return {"ok": True}

    @app.get("/api/tenants/{tenant_id}/knowledge/{knowledge_id}", tags=["knowledge"])
    def get_knowledge(
        tenant_id: UUID, knowledge_id: str, conn: psycopg.Connection = Depends(get_conn)
    ):
        repo = TenantRepository(conn, cipher=load_cipher())
        try:
            backend = backend_for_tenant(tenants=repo, tenant_id=tenant_id)
            knowledge = backend.get(knowledge_id)
        except BackendNotConfigured as exc:
            raise HTTPException(400, str(exc)) from exc
        except KnowledgeBaseError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "id": knowledge.id,
            "title": knowledge.title,
            "body": knowledge.body,
            "url": knowledge.url,
            "labels": list(knowledge.labels),
            "comments": list(knowledge.comments),
        }

    # -------------------------------------------------------------- 通知

    @app.get("/api/tenants/{tenant_id}/notifications",
             response_model=list[NotificationOut], tags=["notifications"])
    def unread(tenant_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
        return [
            NotificationOut(
                id=n.id,
                kb_issue_id=n.kb_issue_id,
                label=n.label,
                title=n.title,
                url=n.url,
            )
            for n in NotificationRepository(conn).unread(tenant_id=tenant_id)
        ]

    @app.post("/api/tenants/{tenant_id}/notifications/read", tags=["notifications"])
    def mark_read(
        tenant_id: UUID, body: MarkReadIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        count = NotificationRepository(conn).mark_read(
            tenant_id=tenant_id, notification_ids=body.ids
        )
        return {"updated": count}

    @app.get("/api/tenants/{tenant_id}/notification-rules", tags=["notifications"])
    def list_rules(tenant_id: UUID, conn: psycopg.Connection = Depends(get_conn)):
        return [
            {"label": r.label, "channel": r.channel, "destination": r.destination}
            for r in NotificationRepository(conn).all_rules(tenant_id=tenant_id)
        ]

    @app.post("/api/tenants/{tenant_id}/notification-rules", tags=["notifications"])
    def add_rule(
        tenant_id: UUID, body: RuleIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        NotificationRepository(conn).add_rule(
            tenant_id=tenant_id,
            label=body.label,
            channel=body.channel,
            destination=body.destination,
        )
        return {"ok": True}

    @app.delete("/api/tenants/{tenant_id}/notification-rules", tags=["notifications"])
    def remove_rule(
        tenant_id: UUID, body: RuleIn, conn: psycopg.Connection = Depends(get_conn)
    ):
        NotificationRepository(conn).remove_rule(
            tenant_id=tenant_id,
            label=body.label,
            channel=body.channel,
            destination=body.destination,
        )
        return {"ok": True}

    # ------------------------------------------------------------ 会話履歴

    @app.get("/api/tenants/{tenant_id}/conversations",
             response_model=list[ConversationOut], tags=["chat"])
    def list_conversations(
        tenant_id: UUID, account_id: UUID, conn: psycopg.Connection = Depends(get_conn)
    ):
        return [
            ConversationOut(id=c.id, title=c.title)
            for c in ConversationRepository(conn).list_for_account(
                tenant_id=tenant_id, account_id=account_id
            )
        ]

    @app.post("/api/tenants/{tenant_id}/conversations",
              response_model=ConversationOut, tags=["chat"])
    def create_conversation(
        tenant_id: UUID, account_id: UUID, conn: psycopg.Connection = Depends(get_conn)
    ):
        conversation = ConversationRepository(conn).create(
            tenant_id=tenant_id, account_id=account_id
        )
        return ConversationOut(id=conversation.id, title=conversation.title)

    @app.get("/api/conversations/{conversation_id}/messages",
             response_model=list[MessageOut], tags=["chat"])
    def list_messages(
        conversation_id: UUID, conn: psycopg.Connection = Depends(get_conn)
    ):
        return [
            MessageOut(id=m.id, role=m.role, content=m.content)
            for m in ConversationRepository(conn).messages(conversation_id=conversation_id)
        ]

    @app.post("/api/conversations/{conversation_id}/messages",
              response_model=MessageOut, tags=["chat"])
    def add_message(
        conversation_id: UUID,
        body: MessageIn,
        conn: psycopg.Connection = Depends(get_conn),
    ):
        if body.role not in (ROLE_USER, ROLE_ASSISTANT):
            raise HTTPException(400, f"未知の role です: {body.role}")
        repo = ConversationRepository(conn)
        message = repo.add_message(
            conversation_id=conversation_id, role=body.role, content=body.content
        )
        # 最初の発言を会話の題名にする（一覧で識別できるようにするため）
        if body.role == ROLE_USER:
            existing = repo.messages(conversation_id=conversation_id)
            if len(existing) == 1:
                repo.set_title(
                    conversation_id=conversation_id, title=body.content[:60]
                )
        return MessageOut(id=message.id, role=message.role, content=message.content)

    @app.delete("/api/conversations/{conversation_id}", tags=["chat"])
    def delete_conversation(
        conversation_id: UUID, conn: psycopg.Connection = Depends(get_conn)
    ):
        ConversationRepository(conn).delete(conversation_id=conversation_id)
        return {"ok": True}

    @app.get("/api/health", tags=["developer"])
    def health(conn: psycopg.Connection = Depends(get_conn)):
        conn.execute("SELECT 1")
        return {"ok": True}

    return app


app = create_app()

