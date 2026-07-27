# 知識ベース検索・登録システム

不具合報告や問い合わせを過去の記録から探し、見つからなければ新しい記録として残していく仕組みです。使うほど記録が蓄積され、次に同じことで困った人が早く解決できるようになります。

![システム全体像](docs/system-overview.png)

> 上図は非エンジニア向けの説明です。ブラウザで見る場合は [docs/system-overview.html](docs/system-overview.html) を開いてください。

## 現在の状態

**設計・要件定義の段階です。実装コードはまだありません。**

主要な設計判断は一通り確定しており、実装に着手できる状態です。ビルド・テストの手順は、実装開始後にこの README に追記します。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/knowledge-base-system-requirements.md](docs/knowledge-base-system-requirements.md) | **要件定義書（正）**。決定内容とその理由を記録 |
| [docs/knowledge-base-system-requirements.html](docs/knowledge-base-system-requirements.html) | 上記の図解版 |
| [docs/system-overview.html](docs/system-overview.html) | 全体像の説明（非エンジニア向け） |
| [docs/issue-similarity-search-design_1.md](docs/issue-similarity-search-design_1.md) | 元の設計メモ（GitLab限定時代）。OCR手法や pgvector のスキーマは現在も有効 |
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
| 差分更新 | cron ポーリングに統一（Redmine が Webhook 非対応のため） |
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

## 検討を保留している事項

いずれも実データや実運用が見えてから判断すべきもので、要件段階では確定させていません。

1. cron ポーリングの実行間隔
2. pgvector のインデックス種別（ivfflat / hnsw）
3. ハイブリッド検索のマージ方式（RRF など）
4. テナントが embedding モデルを変更した際の再embedding運用
