"""knowledge_chunks への読み書き。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import UUID

import psycopg
from psycopg import sql

_SCHEMA = Path(__file__).with_name("schema.sql")

# 検索時に取得するチャンク数。ef_search はこれ以上でなければならない。
DEFAULT_OVERFETCH = 100

# 足切りの距離（`max_distance`）の既定値はここに置かない。**モデルごとに
# 違う値**であり、ここは自分がどのモデルのベクトルを扱っているか知らない。
# 値は EmbeddingProvider.max_distance が持ち、HybridSearch が渡す。


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


@dataclass(frozen=True)
class KnowledgeMeta:
    """検索結果の表示に使う見出し情報。"""

    kb_issue_id: str
    title: str
    url: str | None
    labels: tuple[str, ...] = ()


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _partition_name(tenant_id: UUID) -> str:
    return f"kc_{tenant_id.hex}"


def _index_name(tenant_id: UUID, dim: int) -> str:
    return f"idx_{tenant_id.hex}_{dim}"


def _edge_partition_name(tenant_id: UUID) -> str:
    return f"ke_{tenant_id.hex}"


class ChunkRepository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ DDL

    def create_schema(self) -> None:
        self._conn.execute(_SCHEMA.read_text(encoding="utf-8"))

    def ensure_tenant(self, tenant_id: UUID) -> None:
        """テナント用のパーティションを作る。テナント払い出し時に呼ぶ。"""
        for table, part in (
            ("knowledge_chunks", _partition_name(tenant_id)),
            ("knowledge_edges", _edge_partition_name(tenant_id)),
        ):
            self._conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {part} "
                    "PARTITION OF {table} FOR VALUES IN ({tid})"
                ).format(
                    part=sql.Identifier(part),
                    table=sql.Identifier(table),
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
        max_distance: float | None = None,
    ) -> list[SearchHit]:
        """類似度検索。チャンク単位で多めに取り、知識単位に畳んで返す。

        `max_distance` より遠い知識は返さない。**既定は None（足切りなし）**。
        適切な値は embedding モデル固有なので、ここでは決められない —
        本番の経路では HybridSearch が provider の値を渡す。省略できるのは
        距離の分布そのものを見たいときのためで、検索の既定ではない。
        """
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
                    "HAVING MIN(dist) <= %(max_distance)s "
                    "ORDER BY best LIMIT %(limit)s"
                ).format(dim=sql.Literal(dimensions)),
                {
                    "q": _vector_literal(query_embedding),
                    "tid": tenant_id,
                    "model": model,
                    "overfetch": overfetch,
                    "limit": limit,
                    # 足切りしない指定は、比較が必ず真になる大きな値で表す
                    "max_distance": 99.0 if max_distance is None else max_distance,
                },
            )
            return [SearchHit(r[0], r[1], float(r[2])) for r in cur.fetchall()]

    # ------------------------------------------------------------ metadata

    def remember_knowledge(
        self,
        *,
        tenant_id: UUID,
        kb_issue_id: str,
        title: str,
        url: str | None,
        labels: Sequence[str] = (),
        updated_at: object | None = None,
    ) -> None:
        """検索結果の表示に使う見出し情報を控える。"""
        self._conn.execute(
            "INSERT INTO knowledge_index"
            " (tenant_id, kb_issue_id, title, url, labels, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, kb_issue_id) DO UPDATE SET"
            "   title = EXCLUDED.title, url = EXCLUDED.url,"
            "   labels = EXCLUDED.labels, updated_at = EXCLUDED.updated_at,"
            "   synced_at = now()",
            (tenant_id, kb_issue_id, title, url, list(labels), updated_at),
        )

    def knowledge_meta(
        self, *, tenant_id: UUID, kb_issue_ids: Sequence[str]
    ) -> dict[str, KnowledgeMeta]:
        if not kb_issue_ids:
            return {}
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT kb_issue_id, title, url, labels FROM knowledge_index"
                " WHERE tenant_id = %s AND kb_issue_id = ANY(%s)",
                (tenant_id, list(kb_issue_ids)),
            )
            return {
                row[0]: KnowledgeMeta(
                    kb_issue_id=row[0],
                    title=row[1],
                    url=row[2],
                    labels=tuple(row[3] or ()),
                )
                for row in cur.fetchall()
            }

    # ---------------------------------------------------------------- edges

    def replace_relations(
        self, *, tenant_id: UUID, kb_issue_id: str, related: Sequence[tuple[str, str]]
    ) -> int:
        """1 つの知識が宣言しているつながりを入れ替える。

        行は「誰が宣言したか」の向きで 1 本だけ持ち、両向きには入れない。
        両向きに入れると、片方の知識を同期し直したときに「その知識に触れる
        辺」をまとめて消すことになり、**もう片方が宣言した辺まで巻き添えで
        消える**。探索側（neighbours）で両向きを見ることで無向グラフとして扱う。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM knowledge_edges WHERE tenant_id = %s AND from_issue_id = %s",
                (tenant_id, kb_issue_id),
            )
            if not related:
                return 0
            cur.executemany(
                "INSERT INTO knowledge_edges (tenant_id, from_issue_id, to_issue_id, kind)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, from_issue_id, to_issue_id) DO NOTHING",
                [(tenant_id, kb_issue_id, other, kind) for other, kind in related],
            )
        return len(related)

    def neighbours(
        self, *, tenant_id: UUID, kb_issue_ids: Sequence[str], hops: int = 1
    ) -> dict[str, int]:
        """起点から辿れる知識を {知識ID: ホップ数} で返す。起点自体は含まない。

        辺は宣言した向きで 1 本しか持たないため、両向きを見て無向に辿る。

        既定は 1 ホップ。2 ホップ以上は関連の薄い知識まで引き込み
        ノイズになりやすいため、広げるときは実データで確認すること。
        """
        if hops < 1:
            raise ValueError("hops must be >= 1")
        if not kb_issue_ids:
            return {}

        seen = {str(i) for i in kb_issue_ids}
        frontier = list(seen)
        out: dict[str, int] = {}

        with self._conn.cursor() as cur:
            for hop in range(1, hops + 1):
                if not frontier:
                    break
                cur.execute(
                    "SELECT to_issue_id AS other FROM knowledge_edges"
                    "  WHERE tenant_id = %(tid)s AND from_issue_id = ANY(%(ids)s)"
                    " UNION"
                    " SELECT from_issue_id FROM knowledge_edges"
                    "  WHERE tenant_id = %(tid)s AND to_issue_id = ANY(%(ids)s)",
                    {"tid": tenant_id, "ids": list(frontier)},
                )
                nxt = []
                for (other,) in cur.fetchall():
                    if other in seen:
                        continue
                    seen.add(other)
                    out[other] = hop
                    nxt.append(other)
                frontier = nxt
        return out

    def count_relations(self, *, tenant_id: UUID) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM knowledge_edges WHERE tenant_id = %s", (tenant_id,)
            )
            return int(cur.fetchone()[0])

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
