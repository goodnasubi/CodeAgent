"""knowledge_chunks への読み書き。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql

_SCHEMA = Path(__file__).with_name("schema.sql")

# 検索時に取得するチャンク数。ef_search はこれ以上でなければならない。
DEFAULT_OVERFETCH = 100


@dataclass(frozen=True)
class ChunkRow:
    kb_issue_id: str
    source_name: str | None
    chunk_index: int
    content: str
    embedding: list[float]


@dataclass(frozen=True)
class SearchHit:
    kb_issue_id: str
    source_name: str | None
    distance: float


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _partition_name(tenant_id: UUID) -> str:
    return f"kc_{tenant_id.hex}"


def _index_name(tenant_id: UUID, dim: int) -> str:
    return f"idx_{tenant_id.hex}_{dim}"


class ChunkRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ DDL

    def create_schema(self) -> None:
        self._conn.execute(_SCHEMA.read_text(encoding="utf-8"))

    def ensure_tenant(self, tenant_id: UUID) -> None:
        """テナント用のパーティションを作る。テナント払い出し時に呼ぶ。"""
        self._conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {part} "
                "PARTITION OF knowledge_chunks FOR VALUES IN ({tid})"
            ).format(
                part=sql.Identifier(_partition_name(tenant_id)),
                tid=sql.Literal(str(tenant_id)),
            )
        )

    def ensure_dimension_index(self, tenant_id: UUID, dimensions: int) -> None:
        """次元数ごとの HNSW 部分インデックスを作る。

        インデックスとクエリでキャスト式が一致しないと、エラーを出さずに
        全件走査へ落ちる。式はここと search() の 1 箇所ずつでしか組み立てない。
        """
        self._conn.execute(
            sql.SQL(
                "CREATE INDEX IF NOT EXISTS {name} ON {part} "
                "USING hnsw ((embedding::vector({dim})) vector_cosine_ops) "
                "WHERE embedding_dim = {dim}"
            ).format(
                name=sql.Identifier(_index_name(tenant_id, dimensions)),
                part=sql.Identifier(_partition_name(tenant_id)),
                dim=sql.Literal(dimensions),
            )
        )

    # --------------------------------------------------------------- writes

    def replace_issue_chunks(
        self,
        *,
        tenant_id: UUID,
        kb_issue_id: str,
        rows: list[ChunkRow],
        model: str,
        dimensions: int,
    ) -> int:
        """1 つの知識に属するチャンクを入れ替える。

        追記や KB 側での編集を取り込む際、古いチャンクが残ると検索結果が
        二重になるため、同じ知識の同じモデルの行を削除してから挿入する。
        別モデルの行は消さない（モデル切り替え中は新旧が併存するため）。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM knowledge_chunks "
                "WHERE tenant_id = %s AND kb_issue_id = %s AND embedding_model = %s",
                (tenant_id, kb_issue_id, model),
            )
            if not rows:
                return 0
            cur.executemany(
                "INSERT INTO knowledge_chunks "
                "(tenant_id, kb_issue_id, source_name, chunk_index, content,"
                " embedding, embedding_model, embedding_dim) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        tenant_id,
                        kb_issue_id,
                        r.source_name,
                        r.chunk_index,
                        r.content,
                        _vector_literal(r.embedding),
                        model,
                        dimensions,
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def delete_model_rows(self, *, tenant_id: UUID, model: str) -> int:
        """特定モデルの行を削除する。再embedding 完了後の後片付けに使う。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM knowledge_chunks WHERE tenant_id = %s AND embedding_model = %s",
                (tenant_id, model),
            )
            return cur.rowcount

    # --------------------------------------------------------------- search

    def search(
        self,
        *,
        tenant_id: UUID,
        query_embedding: list[float],
        model: str,
        limit: int = 10,
        overfetch: int = DEFAULT_OVERFETCH,
    ) -> list[SearchHit]:
        """類似度検索。チャンク単位で多めに取り、知識単位に畳んで返す。"""
        dimensions = len(query_embedding)
        if overfetch < limit:
            raise ValueError("overfetch must be >= limit")

        # ef_search は再現率だけでなく「返せる行数の上限」でもある。既定の 40 の
        # ままだと overfetch を大きくしても 40 件しか返らない。
        #
        # SET LOCAL はパラメータを取れないため set_config() を使う。また
        # SET LOCAL の効果はトランザクション内に限られ、autocommit 接続では
        # 次の文まで残らないため、検索と同じトランザクションに入れる。
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('hnsw.ef_search', %s, true)",
                (str(max(overfetch, 40)),),
            )
            cur.execute(
                sql.SQL(
                    "WITH hits AS ("
                    "  SELECT kb_issue_id, source_name,"
                    "         embedding::vector({dim}) <=> %(q)s::vector({dim}) AS dist"
                    "  FROM knowledge_chunks"
                    "  WHERE tenant_id = %(tid)s"
                    "    AND embedding_dim = {dim}"
                    "    AND embedding_model = %(model)s"
                    "  ORDER BY embedding::vector({dim}) <=> %(q)s::vector({dim})"
                    "  LIMIT %(overfetch)s"
                    ") "
                    "SELECT kb_issue_id, source_name, MIN(dist) AS best "
                    "FROM hits GROUP BY kb_issue_id, source_name "
                    "ORDER BY best LIMIT %(limit)s"
                ).format(dim=sql.Literal(dimensions)),
                {
                    "q": _vector_literal(query_embedding),
                    "tid": tenant_id,
                    "model": model,
                    "overfetch": overfetch,
                    "limit": limit,
                },
            )
            return [SearchHit(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    def count(self, *, tenant_id: UUID, model: str | None = None) -> int:
        with self._conn.cursor() as cur:
            if model is None:
                cur.execute(
                    "SELECT count(*) FROM knowledge_chunks WHERE tenant_id = %s",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT count(*) FROM knowledge_chunks "
                    "WHERE tenant_id = %s AND embedding_model = %s",
                    (tenant_id, model),
                )
            return int(cur.fetchone()[0])
