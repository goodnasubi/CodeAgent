# GitLabイシュー類似度検索 設計メモ

## 背景・目的

self-hosted GitLabのイシューをLLMから検索できるようにする。まずはキーワード検索、最終的には類似度検索(embedding)まで実現したい。

## 前提とする既存環境

- self-hosted GitLab(バージョン要確認。イシュー検索用の公式MCPツール `gitlab_issue_search` はまだ発展途上のため、現時点では代替手段を使う)
- Claude Code から Bash 経由で `glab` CLI が利用可能(`glab auth login` 済み想定)
- Vertex AI(`google-genai` SDK、`asia-northeast1`、`gemini-2.5-flash` 利用実績あり)
- PostgreSQL が既存MCP構成に組み込み済み
- Redmine / GitLab / PostgreSQL / Lark の MCP構成が既にある

## 検索手段の結論

GitLab公式MCPサーバーはバージョン依存で機能が未成熟なため、`glab` コマンドをBash経由で叩く方針を採用。

```bash
# キーワード検索(単一プロジェクト)
glab issue list -R group/project -S "キーワード" --output json

# GitLab Search API 経由(プロジェクト/グループ横断も可能)
glab api "search?scope=issues&search=キーワード" | jq
```

- MCPサーバーのバージョン対応待ちが不要
- 認証は `glab auth login` で完結
- 単発利用なら十分。継続的にRedmine連携のようなワークフローに組み込むなら、将来的に自前MCPツール化を検討

## 類似度検索(embedding)の設計

### 採用方針

テキストと画像(OCR)を**統合する案**を採用。
画像そのもののvisual embedding(`multimodalembedding@001`)は使わず、画像内のOCRテキストを本文に結合してから単一のテキストembeddingモデルで処理する。理由: embeddingモデルを1系統に統一でき、pgvectorのスキーマ・運用がシンプルになるため。

### パイプライン全体像

```
画像取得(glab api /uploads/ 経由でダウンロード)
  → OCR(Gemini gemini-2.5-flash、またはCloud Vision API の DOCUMENT_TEXT_DETECTION)
  → イシュー本文 + OCRテキストを結合(区切りマーカーで分離しておく)
  → text-embedding-005 でembedding化
  → pgvectorに格納
```

### OCR手段の比較

| 手段                                      | 特徴                                                          |
| ----------------------------------------- | ------------------------------------------------------------- |
| Gemini(マルチモーダル)                    | レイアウト崩れ・日本語混在に強い。追加API有効化不要。実装が楽 |
| Cloud Vision API(DOCUMENT_TEXT_DETECTION) | OCR特化、座標・行構造も取得可。コスト効率が良い場合が多い     |

スクリーンショット・エラーログ中心の用途ならGemini、大量枚数の機械的処理ならVision API向き。

### DBスキーマ(pgvector)

```sql
CREATE TABLE issue_embeddings (
  id SERIAL PRIMARY KEY,
  issue_id INT,
  combined_text TEXT,      -- 本文 + OCR結果(マーカーで区切り)
  embedding VECTOR(768)    -- text-embedding-005 の次元数
);
```

結合テキストの例:

```
<issue本文>

[画像OCR: filename.png]
<OCR抽出テキスト>
```

### 検索クエリ例

```sql
SELECT issue_id, combined_text, 1 - (embedding <=> :query_vector) AS similarity
FROM issue_embeddings
ORDER BY embedding <=> :query_vector
LIMIT 10;
```

### 運用上の注意点

- **差分更新**: 新規イシュー・更新分のみ処理(GitLab Webhookトリガー、または定期cron)。全件再embeddingは避ける
- **トークン上限**: text-embedding-005は概ね2048トークン程度が目安。超える場合はOCR部分を要約するかchunk分割
- **OCR誤認識対策**: 信頼度スコアが取得できる場合(Vision API)は保持し、後でフィルタに使えるようにする
- **添付画像の抽出元**: イシュー本文中のmarkdown `/uploads/xxx/filename.png` パスを正規表現で抽出し、`glab api "projects/:id/uploads/xxx/filename.png"` でダウンロード

## 未確定・次に詰める点

1. self-hosted GitLabの正確なバージョン確認(公式MCPの `gitlab_issue_search` が使えるかの判断材料)
2. OCR手段の最終選択(Gemini vs Cloud Vision API)
3. OCR→結合→embeddingのバッチ処理実装(cron / Webhookどちらで差分更新するか)
4. pgvectorのインデックス設計(ivfflat / hnsw等、データ量next第で選定)
