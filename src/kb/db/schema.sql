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


-- 知識どうしのつながり。KB 側が持つ関係（GitHub の相互参照、GitLab の
-- 関連イシュー、Redmine のイシュー関連）を写した派生データ。
-- embedding と同じく、捨てて KB から作り直せる。
--
-- 行は「どの知識が宣言したつながりか」の向きで 1 本だけ持つ。両向きに
-- 入れると、片方を同期し直したときにもう片方が宣言した辺まで巻き添えで
-- 消えるため。探索側で両向きを見て無向グラフとして扱う。
CREATE TABLE IF NOT EXISTS knowledge_edges (
  tenant_id    UUID        NOT NULL,
  from_issue_id TEXT       NOT NULL,
  to_issue_id  TEXT        NOT NULL,
  kind         TEXT        NOT NULL DEFAULT 'references',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, from_issue_id, to_issue_id)
) PARTITION BY LIST (tenant_id);
