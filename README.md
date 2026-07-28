# 知識ベース検索・登録システム

不具合報告や問い合わせを過去の記録から探し、見つからなければ新しい記録として残していく仕組みです。使うほど記録が蓄積され、次に同じことで困った人が早く解決できるようになります。

![システム全体像](docs/system-overview.png)

> 上図は非エンジニア向けの説明です。ブラウザで見る場合は [docs/system-overview.html](docs/system-overview.html) を開いてください。

## 現在の状態

**設計・要件定義の段階です。アプリケーションコードはまだありません。**

設計上の論点は17件すべて確定しており、実装に着手できる状態です。設計の中核となる pgvector の構成（テナント別パーティション + 次元別部分インデックス）は、実際の PostgreSQL 17 + pgvector 0.8.5 で動作を確認済みです（[verify/](verify/)）。

ビルド・テストの手順は、実装開始後にこの README に追記します。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/knowledge-base-system-requirements.md](docs/knowledge-base-system-requirements.md) | **要件定義書（正）**。決定内容とその理由を記録 |
| [docs/knowledge-base-system-requirements.html](docs/knowledge-base-system-requirements.html) | 上記の図解版 |
| [docs/system-overview.html](docs/system-overview.html) | 全体像の説明（非エンジニア向け） |
| [docs/issue-similarity-search-design_1.md](docs/issue-similarity-search-design_1.md) | 元の設計メモ（GitLab限定時代）。OCR手法や pgvector のスキーマは現在も有効 |
| [verify/README.md](verify/README.md) | 設計検証スクリプトと、開発環境の構築手順 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 向けの作業ガイド |

## 主要な設計判断

詳しい理由は[要件定義書](docs/knowledge-base-system-requirements.md)に記載しています。

### 知識の実体は外部KB側にある

**GitLab / GitHub / Redmine が知識の正（source of truth）であり、pgvector は検索用インデックス**です。新規知識はKB側にIssue・チケットとして作成され、pgvector にはそこから作られた派生データが入ります。

この前提から、pgvector は捨てて再構築できる（バックアップ不要）こと、KB API の障害時は新規登録が止まること、ラベルがKBネイティブ機能として使えることが導かれます。他の判断の多くがこれに依存しています。

### その他

| 項目 | 決定 |
|---|---|
| 知識ベース | テナントごとに1つ選択（GitLab / GitHub / Redmine の同時併用はしない） |
| LLM / embedding | どちらもテナントごとに選択可能（GPT-5.x / Gemini / Claude API） |
| 検索方式 | 類似度検索（pgvector）+ キーワード検索（KB API）のハイブリッド |
| ベクトル索引 | HNSW。チャンクテーブルはテナントごとにパーティション分割 |
| 差分更新 | cron ポーリング（10分間隔）に統一。Redmine が Webhook 非対応のため |
| 検索結果のマージ | RRF（Reciprocal Rank Fusion）。順位のみを使うためスコア正規化が不要 |
| 通知 | ラベル付与がトリガー。チャット画面 / Slack / メールへ配信 |
| 認証 | 管理者が払い出す識別子で運用（後から独自認証・IdP連携を追加可能） |

> **注意**: 認証方式は**社内クローズド運用**を前提としています。インターネット公開する場合はこの前提が崩れるため、実装着手時点で認証基盤が必須になります。

## 技術スタック（予定）

| 領域 | 採用 |
|---|---|
| バックエンド | Python 3.10+ |
| フロントエンド | TypeScript + React |
| データベース | PostgreSQL + pgvector |
| ドキュメント変換 | [markitdown](https://github.com/microsoft/markitdown) |

## 実装後に調整するパラメータ

設計上の論点は17件すべて決定済みです。以下は方式が決まっており、実データを見て数値だけを調整する項目です。

| 項目 | 初期値 | 調整の判断材料 |
|---|---|---|
| cron ポーリング間隔 | 10分 | KB側で直接編集される実際の頻度 |
| RRF の定数 `k` | 60（慣例値） | 類似度検索とキーワード検索のどちらを効かせたいか |
| `hnsw.ef_search` | 40（既定値） | 検索の再現率と応答速度のバランス |
