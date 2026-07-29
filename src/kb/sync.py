"""KB からの取り込み（cron ポーリング）。

    KB の更新分を取得 → つながりを取得 → embedding して格納 → 通知を配る

Webhook ではなくポーリングなのは、Redmine が Webhook を標準で持たず、
全バックエンドで同じ仕組みに揃えられるのがポーリングだけのため。

この間隔が決めているのは「検索結果の鮮度」ではなく、**KB 側で直接編集された
内容が反映されるまでの遅れ**である。本システム経由で登録した知識はその場で
embedding するので、ここが拾うのは KB を直接触られたケースに限られる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from .backends.base import Knowledge, KnowledgeBase, KnowledgeBaseError
from .db.sync import SyncStateRepository
from .ingest import IngestPipeline
from .notifications import NotificationDispatcher

logger = logging.getLogger(__name__)

# 初回取り込みの起点。KB にある既存の知識をすべて対象にする。
#
# **Unix エポック（1970-01-01）は使えない。** GitHub はその日時を渡すと
# エラーも出さずに 0 件を返す（タイムスタンプ 0 を「未指定」として扱って
# いるとみられる。1990 年以降なら正常に返る）。新規テナントの初回取り込みが
# 黙って空になる不具合になるため、十分に過去かつ実在する日付を使う。
# 2000 年は GitHub(2008)・GitLab(2011)・Redmine(2006) のいずれの誕生より前。
INITIAL_SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)

# 旧名。既存の呼び出しのために残す。
EPOCH = INITIAL_SINCE


@dataclass
class SyncReport:
    tenant_id: UUID
    since: datetime
    ingested: list[str] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    """(知識 ID, エラー内容)。"""
    aborted: str | None = None
    """取得そのものに失敗した場合の理由。"""

    @property
    def ok(self) -> bool:
        return self.aborted is None and not self.failed


class SyncRunner:
    def __init__(
        self,
        *,
        pipeline: IngestPipeline,
        backend: KnowledgeBase,
        state: SyncStateRepository,
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._backend = backend
        self._state = state
        self._dispatcher = dispatcher

    def sync(self, *, tenant_id: UUID, since: datetime | None = None) -> SyncReport:
        """更新分を取り込む。

        取得の起点は、明示指定 > 前回の到達点 > INITIAL_SINCE（初回は全件）の順。

        **時刻は取得を始める前に確定させる。** 取得中に更新された知識は
        次回に回るが、取り終えた時刻を使うとその分を取りこぼす。
        """
        started_at = datetime.now(timezone.utc)
        state = self._state.get(tenant_id=tenant_id)
        start_from = since or (state.last_synced_at if state else None) or INITIAL_SINCE
        report = SyncReport(tenant_id=tenant_id, since=start_from)

        try:
            batch = list(self._backend.updated_since(start_from))
        except KnowledgeBaseError as exc:
            # 取得できなければ何も進めない。次回もう一度同じ範囲を取りに行く
            logger.warning("同期を中止（KB から取得できず）: %s", exc)
            report.aborted = str(exc)
            self._state.record_failure(tenant_id=tenant_id, error=str(exc))
            return report

        for knowledge in batch:
            try:
                self._sync_one(tenant_id=tenant_id, knowledge=knowledge, report=report)
            except Exception as exc:  # 1 件の失敗で残りを止めない
                logger.warning("知識の取り込みに失敗 id=%s: %s", knowledge.id, exc)
                report.failed.append((knowledge.id, str(exc)))

        if report.failed:
            # 取りこぼした知識があるので到達点は進めない。次回やり直す。
            # 成功した分は取り込み済みなので、やり直しても二重にはならない
            # （同一モデルの行を置き換える作りのため）
            self._state.record_failure(
                tenant_id=tenant_id,
                error=f"{len(report.failed)} 件の取り込みに失敗",
            )
        else:
            self._state.record_success(tenant_id=tenant_id, synced_at=started_at)

        return report

    def _sync_one(
        self, *, tenant_id: UUID, knowledge: Knowledge, report: SyncReport
    ) -> None:
        relations = []
        if self._backend.supports_relations:
            try:
                relations = self._backend.relations(knowledge.id)
            except KnowledgeBaseError as exc:
                # つながりが取れなくても本体は取り込む。検索の 1 信号が
                # 欠けるだけで、知識そのものは失われない
                logger.info("つながりを取得できず id=%s: %s", knowledge.id, exc)

        self._pipeline.ingest_knowledge(
            tenant_id=tenant_id, knowledge=knowledge, relations=relations
        )
        report.ingested.append(knowledge.id)

        if self._dispatcher is not None:
            result = self._dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge)
            if result.delivered:
                report.notified.append(knowledge.id)
