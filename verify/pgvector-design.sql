-- pgvector 設計検証スクリプト
--
-- 確認すること:
--   1. 次元数の異なるベクトルを同一カラムに格納できるか
--   2. テナント別パーティション + 次元別部分インデックスで HNSW が使われるか
--   3. キャスト式が揃わないと全件走査に落ちること（落とし穴の実証）
--
-- 実行方法は verify/README.md を参照。

\timing off
\set ON_ERROR_STOP on

DROP TABLE IF EXISTS knowledge_chunks CASCADE;

-- 設計どおり: tenant_id でパーティション、embedding は次元数を固定しない
CREATE TABLE knowledge_chunks (
  id              BIGSERIAL,
  tenant_id       UUID   NOT NULL,
  kb_issue_id     TEXT   NOT NULL,
  source_name     TEXT,
  chunk_index     INT    NOT NULL,
  content         TEXT   NOT NULL,
  embedding       vector NOT NULL,
  embedding_model TEXT   NOT NULL,
  embedding_dim   INT    NOT NULL,
  PRIMARY KEY (tenant_id, id)
) PARTITION BY LIST (tenant_id);

CREATE TABLE kc_t1 PARTITION OF knowledge_chunks
  FOR VALUES IN ('11111111-1111-1111-1111-111111111111');
CREATE TABLE kc_t2 PARTITION OF knowledge_chunks
  FOR VALUES IN ('22222222-2222-2222-2222-222222222222');

-- ランダムベクトル生成
CREATE OR REPLACE FUNCTION rndvec(d int) RETURNS vector AS $$
  SELECT ('[' || string_agg(random()::text, ',') || ']')::vector
  FROM generate_series(1, d);
$$ LANGUAGE sql VOLATILE;

-- テナント1: 768次元を4000行（1知識=4チャンク）
INSERT INTO knowledge_chunks
  (tenant_id, kb_issue_id, source_name, chunk_index, content, embedding, embedding_model, embedding_dim)
SELECT '11111111-1111-1111-1111-111111111111',
       'issue-' || (i/4), 'file-' || (i/4) || '.xlsx', i%4,
       'chunk ' || i, rndvec(768), 'text-embedding-005', 768
FROM generate_series(1, 4000) i;

-- テナント2: 1536次元を1000行（次元混在の確認）
INSERT INTO knowledge_chunks
  (tenant_id, kb_issue_id, source_name, chunk_index, content, embedding, embedding_model, embedding_dim)
SELECT '22222222-2222-2222-2222-222222222222',
       'issue-' || (i/4), 'file-' || (i/4) || '.docx', i%4,
       'chunk ' || i, rndvec(1536), 'text-embedding-3-small', 1536
FROM generate_series(1, 1000) i;

\echo '=== 格納状況（次元混在が同一カラムに同居できているか）==='
SELECT tenant_id, embedding_model, embedding_dim, count(*)
FROM knowledge_chunks GROUP BY 1,2,3 ORDER BY 3;

-- 次元数ごとの部分インデックス（パーティションごとに張る）
CREATE INDEX idx_t1_768 ON kc_t1
  USING hnsw ((embedding::vector(768)) vector_cosine_ops)
  WHERE embedding_dim = 768;

CREATE INDEX idx_t2_1536 ON kc_t2
  USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
  WHERE embedding_dim = 1536;

ANALYZE knowledge_chunks;

\echo ''
\echo '=== 検証1: 正しいクエリ（キャスト式が一致）→ HNSW が使われるか ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
WITH hits AS (
  SELECT kb_issue_id, source_name,
         embedding::vector(768) <=> (SELECT rndvec(768)) AS dist
  FROM knowledge_chunks
  WHERE tenant_id = '11111111-1111-1111-1111-111111111111'
    AND embedding_dim = 768
  ORDER BY embedding::vector(768) <=> (SELECT rndvec(768))
  LIMIT 100
)
SELECT kb_issue_id, source_name, MIN(dist) AS best_dist
FROM hits GROUP BY kb_issue_id, source_name ORDER BY best_dist LIMIT 10;

\echo ''
\echo '=== 検証2: キャストを外した場合 → 全件走査に落ちるか（落とし穴の実証）==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
SELECT kb_issue_id
FROM knowledge_chunks
WHERE tenant_id = '11111111-1111-1111-1111-111111111111'
  AND embedding_dim = 768
ORDER BY embedding <=> (SELECT rndvec(768))
LIMIT 10;

\echo ''
\echo '=== 検証3: テナント指定でパーティション枝刈りが効くか ==='
\echo '（kc_t2 のみが走査対象になること。行数1000では Seq Scan が選ばれるが正常）'
EXPLAIN (COSTS OFF)
SELECT kb_issue_id
FROM knowledge_chunks
WHERE tenant_id = '22222222-2222-2222-2222-222222222222'
  AND embedding_dim = 1536
ORDER BY embedding::vector(1536) <=> (SELECT rndvec(1536))
LIMIT 10;

\echo ''
\echo '=== 検証4: ef_search がオーバーフェッチ件数の上限になること ==='
\echo '--- ef_search 既定(40) で LIMIT 100 を要求 → 40 件しか返らない ---'
SELECT count(*) AS rows_returned FROM (
  SELECT 1 FROM knowledge_chunks
  WHERE tenant_id = '11111111-1111-1111-1111-111111111111' AND embedding_dim = 768
  ORDER BY embedding::vector(768) <=> (SELECT rndvec(768)) LIMIT 100) t;

SET hnsw.ef_search = 200;
\echo '--- ef_search = 200 で LIMIT 100 を要求 → 100 件返る ---'
SELECT count(*) AS rows_returned FROM (
  SELECT 1 FROM knowledge_chunks
  WHERE tenant_id = '11111111-1111-1111-1111-111111111111' AND embedding_dim = 768
  ORDER BY embedding::vector(768) <=> (SELECT rndvec(768)) LIMIT 100) t;
RESET hnsw.ef_search;

\echo ''
\echo '=== 検証5: 少ない行数でもインデックス自体は正常であること ==='
\echo '（Seq Scan を禁止すると idx_t2_1536 が使われる = インデックスの不具合ではない）'
SET enable_seqscan = off;
EXPLAIN (COSTS OFF)
SELECT kb_issue_id
FROM knowledge_chunks
WHERE tenant_id = '22222222-2222-2222-2222-222222222222'
  AND embedding_dim = 1536
ORDER BY embedding::vector(1536) <=> (SELECT rndvec(1536))
LIMIT 10;
RESET enable_seqscan;
