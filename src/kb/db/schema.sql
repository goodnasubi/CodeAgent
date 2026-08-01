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
-- テナント。管理者が払い出す。利用者は自分で作れない。
CREATE TABLE IF NOT EXISTS tenants (
  id         UUID        PRIMARY KEY,
  name       TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- 知識ベースへの接続情報。テナント本体とは分けて持つ。
--
-- トークンのローテーション・有効/無効・接続テストの結果といった、接続固有の
-- 状態が後から生えてくる。またテナント本体は認可チェックで頻繁に読むのに対し
-- トークンは KB を呼ぶときにしか要らないので、分けておくと不用意に
-- SELECT しない実装がしやすい。
CREATE TABLE IF NOT EXISTS tenant_kb_connections (
  tenant_id       UUID        PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  kb_type         TEXT        NOT NULL,  -- github / gitlab / redmine / relation
  base_url        TEXT,                  -- self-hosted 用。省略時は各既定値
  project         TEXT        NOT NULL,  -- owner/repo, group/project, 識別子など
  encrypted_token BYTEA       NOT NULL,  -- アプリ側で暗号化してから保存する
  extra           JSONB       NOT NULL DEFAULT '{}',  -- message_box_id など
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- LLM / embedding の選択。どちらもテナントごとに選べる。
--
-- API キーは KB のトークンと同じくアプリ側で暗号化して保存し、レスポンスには
-- 決して載せない。**LLM と embedding で別のプロバイダを選べる**ため鍵は 2 本
-- 持つ（同じプロバイダを選んだ場合は同じ値が 2 つ入る）。
CREATE TABLE IF NOT EXISTS tenant_model_settings (
  tenant_id          UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  llm_provider       TEXT NOT NULL DEFAULT 'claude',
  llm_model          TEXT NOT NULL DEFAULT '',
  embedding_provider TEXT NOT NULL DEFAULT 'hashing',
  embedding_model    TEXT NOT NULL DEFAULT 'hashing-dev',
  embedding_dim      INT  NOT NULL DEFAULT 768,
  encrypted_llm_api_key       BYTEA,
  encrypted_embedding_api_key BYTEA,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 既に作られている DB にも列を足す（マイグレーション機構は持たず、
-- schema.sql を何度流しても同じ状態になるようにしてある）。
ALTER TABLE tenant_model_settings
  ADD COLUMN IF NOT EXISTS encrypted_llm_api_key BYTEA;
ALTER TABLE tenant_model_settings
  ADD COLUMN IF NOT EXISTS encrypted_embedding_api_key BYTEA;


-- アカウント。管理者が払い出す識別子で運用する（独自認証は持たない）。
CREATE TABLE IF NOT EXISTS accounts (
  id           UUID        PRIMARY KEY,
  tenant_id    UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  display_name TEXT        NOT NULL DEFAULT '',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_accounts_tenant ON accounts (tenant_id);


-- 会話履歴。**DB が正**で、localStorage はキャッシュ。
-- 端末を跨いでも同じ会話を続けられるようにするため。
CREATE TABLE IF NOT EXISTS conversations (
  id         UUID        PRIMARY KEY,
  tenant_id  UUID        NOT NULL,
  account_id UUID        NOT NULL,
  title      TEXT        NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversations_account
  ON conversations (tenant_id, account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT        NOT NULL,  -- user / assistant
  content         TEXT        NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages
  ON conversation_messages (conversation_id, created_at);


-- ポーリングの進捗。どこまで取り込んだかを覚えておく。
CREATE TABLE IF NOT EXISTS sync_state (
  tenant_id      UUID        PRIMARY KEY,
  last_synced_at TIMESTAMPTZ,          -- ここまでの更新は取り込み済み
  last_run_at    TIMESTAMPTZ,
  last_error     TEXT
);


-- 取り込みに失敗した知識と、その連続失敗回数。
--
-- **1 件の恒久的な失敗で取り込み全体が止まらないようにするために要る。**
-- 到達点は「完全に成功した回」だけ進める作りなので、常に失敗する知識が
-- 1 件あると到達点が永久に動かず、他の新しい知識も毎回取り直しになる。
-- 回数を数えておき、一定回数を超えたものは隔離して到達点を進める。
--
-- 隔離しても KB 側で編集されれば updated_since が再び返すので、直せば
-- 次の取り込みで自然に復帰する（成功したら行を消す）。
CREATE TABLE IF NOT EXISTS sync_failures (
  tenant_id       UUID        NOT NULL,
  kb_issue_id     TEXT        NOT NULL,
  attempts        INT         NOT NULL DEFAULT 1,
  last_error      TEXT,
  first_failed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_failed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kb_issue_id)
);


-- 知識 1 件ぶんの見出し情報。検索結果の表示に使う派生データ。
--
-- 検索のたびに KB へ問い合わせると N+1 になり、レート制限の厳しい
-- バックエンド（Re:lation は 60 リクエスト/分）では破綻する。取り込み時に
-- こちらへ写しておくことで、KB が落ちていても検索結果を出せる。
CREATE TABLE IF NOT EXISTS knowledge_index (
  tenant_id   UUID        NOT NULL,
  kb_issue_id TEXT        NOT NULL,
  title       TEXT        NOT NULL DEFAULT '',
  url         TEXT,
  labels      TEXT[]      NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kb_issue_id)
);


-- ラベル → 通知先の対応。テナント管理者が管理設定画面で定義する。
-- これは派生データではなく設定なので、KB から作り直すことはできない。
CREATE TABLE IF NOT EXISTS notification_rules (
  tenant_id   UUID        NOT NULL,
  label       TEXT        NOT NULL,
  channel     TEXT        NOT NULL,  -- in_app / slack / email
  destination TEXT        NOT NULL,  -- Slack の Webhook URL、メールアドレスなど
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, label, channel, destination)
);


-- 前回の同期時点で、その知識に付いていたラベル。
--
-- 「ラベルが付いた」ことを検知するには前回との差分が要る。10 分ごとの
-- ポーリングで同じラベルを何度も通知しないための土台でもある。
CREATE TABLE IF NOT EXISTS knowledge_label_state (
  tenant_id   UUID        NOT NULL,
  kb_issue_id TEXT        NOT NULL,
  labels      TEXT[]      NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, kb_issue_id)
);


-- アプリ内通知の受信箱。チャット画面がここを読む。
CREATE TABLE IF NOT EXISTS app_notifications (
  id          BIGSERIAL,
  tenant_id   UUID        NOT NULL,
  kb_issue_id TEXT        NOT NULL,
  label       TEXT        NOT NULL,
  title       TEXT        NOT NULL,
  url         TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  read_at     TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS idx_app_notifications_unread
  ON app_notifications (tenant_id, created_at DESC) WHERE read_at IS NULL;

-- 上の 3 つはパーティション分割しない。knowledge_chunks を分割したのは
-- 近似インデックスの WHERE がインデックス走査の後に効くためで、通常の
-- B-tree インデックスにはその問題が無い。


CREATE TABLE IF NOT EXISTS knowledge_edges (
  tenant_id    UUID        NOT NULL,
  from_issue_id TEXT       NOT NULL,
  to_issue_id  TEXT        NOT NULL,
  kind         TEXT        NOT NULL DEFAULT 'references',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, from_issue_id, to_issue_id)
) PARTITION BY LIST (tenant_id);
