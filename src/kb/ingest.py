"""取り込みパイプライン: ドキュメント → Markdown → チャンク → embedding → 格納。

知識の正は外部 KB 側にあり、ここで作るのは検索用の派生データ。
そのため kb_issue_id（KB 側の Issue / チケット ID）を必ず伴う。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import UUID

from . import chunking
from .backends.base import Knowledge, Relation
from .db.repository import ChunkRepository, ChunkRow
from .documents import ConvertedDocument, DocumentConverter
from .embeddings import EmbeddingProvider


@dataclass(frozen=True)
class IngestResult:
    kb_issue_id: str
    source_name: str
    chunks: int
    model: str
    dimensions: int


class IngestPipeline:
    def __init__(
        self,
        *,
        repository: ChunkRepository,
        embedder: EmbeddingProvider,
        converter: DocumentConverter | None = None,
        max_chars: int = 1500,
        overlap: int = 150,
    ) -> None:
        self._repo = repository
        self._embedder = embedder
        self._converter = converter or DocumentConverter()
        self._max_chars = max_chars
        self._overlap = overlap

    def ingest_text(
        self,
        *,
        tenant_id: UUID,
        kb_issue_id: str,
        text: str,
        source_name: str = "",
    ) -> IngestResult:
        """すでにテキスト化された内容を取り込む。

        チャンクが 0 件でも（空文書）既存行の削除は行う。KB 側で内容が
        空にされた場合に古いチャンクが残らないようにするため。
        """
        chunks = chunking.split(text, max_chars=self._max_chars, overlap=self._overlap)
        vectors = self._embedder.embed([c.text for c in chunks]) if chunks else []

        rows = [
            ChunkRow(
                kb_issue_id=kb_issue_id,
                source_name=source_name or None,
                chunk_index=c.index,
                content=c.text,
                embedding=v,
            )
            for c, v in zip(chunks, vectors)
        ]

        self._repo.ensure_tenant(tenant_id)
        self._repo.ensure_dimension_index(tenant_id, self._embedder.dimensions)
        self._repo.replace_issue_chunks(
            tenant_id=tenant_id,
            kb_issue_id=kb_issue_id,
            rows=rows,
            model=self._embedder.model,
            dimensions=self._embedder.dimensions,
        )

        return IngestResult(
            kb_issue_id=kb_issue_id,
            source_name=source_name,
            chunks=len(rows),
            model=self._embedder.model,
            dimensions=self._embedder.dimensions,
        )

    def ingest_knowledge(
        self,
        *,
        tenant_id: UUID,
        knowledge: Knowledge,
        relations: Sequence[Relation] = (),
    ) -> IngestResult:
        """KB から取得した知識を丸ごと取り込む。同期の入口。

        本文だけでなくタイトル・コメント・添付の抽出結果までを 1 つの
        テキストにまとめて embedding する（`combined_text`）。あわせて
        検索結果の表示に使う見出し情報と、知識どうしのつながりも控える。
        """
        result = self.ingest_text(
            tenant_id=tenant_id,
            kb_issue_id=knowledge.id,
            text=knowledge.combined_text(),
            source_name=knowledge.title,
        )
        self._repo.remember_knowledge(
            tenant_id=tenant_id,
            kb_issue_id=knowledge.id,
            title=knowledge.title,
            url=knowledge.url or None,
            labels=knowledge.labels,
            updated_at=knowledge.updated_at,
        )
        self._repo.replace_relations(
            tenant_id=tenant_id,
            kb_issue_id=knowledge.id,
            related=[(r.to_id, r.kind) for r in relations],
        )
        return result

    def ingest_file(
        self, *, tenant_id: UUID, kb_issue_id: str, path: str | Path
    ) -> IngestResult:
        doc = self._converter.convert_path(path)
        return self._ingest_document(tenant_id=tenant_id, kb_issue_id=kb_issue_id, doc=doc)

    def ingest_upload(
        self, *, tenant_id: UUID, kb_issue_id: str, data: bytes, filename: str
    ) -> IngestResult:
        doc = self._converter.convert_bytes(data, filename=filename)
        return self._ingest_document(tenant_id=tenant_id, kb_issue_id=kb_issue_id, doc=doc)

    def ingest_url(self, *, tenant_id: UUID, kb_issue_id: str, url: str) -> IngestResult:
        doc = self._converter.convert_url(url)
        return self._ingest_document(tenant_id=tenant_id, kb_issue_id=kb_issue_id, doc=doc)

    def _ingest_document(
        self, *, tenant_id: UUID, kb_issue_id: str, doc: ConvertedDocument
    ) -> IngestResult:
        return self.ingest_text(
            tenant_id=tenant_id,
            kb_issue_id=kb_issue_id,
            text=doc.text,
            source_name=doc.source_name,
        )
