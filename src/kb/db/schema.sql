-- 知識チャンクの格納先。
--
-- 設計上の要点（verify/pgvector-design.sql で実環境検証済み）:
--   * embedding は次元数を固定しない。テナントごとに embedding モデルが
--     異なり、次元数も揃わないため。
--   * tenant_id でパーティション分割する。近似インデックスでは WHERE 句が
--     インデックス走査の「後」に適用されるため、通常のフィルタでは
--     テナント絞り込みで結果がほぼ空になる。
--   * HNSW インデックスは次元数ごとに部分インデックスとして張る
--     （パーティション単位。ensure_tenant / ensure_dimension_index を参照）。

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id              BIGSERIAL,
  tenant_id       UUID        NOT NULL,
  kb_issue_id     TEXT        NOT NULL,  -- KB 側の Issue / チケット ID（正は KB 側）
  source_name     TEXT,                  -- 元ファイル名・URL
  chunk_index     INT         NOT NULL,
  content         TEXT        NOT NULL,
  embedding       vector      NOT NULL,
  embedding_model TEXT        NOT NULL,
  embedding_dim   INT         NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
) PARTITION BY LIST (tenant_id);
