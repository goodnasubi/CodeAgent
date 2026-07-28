# 設計検証

実装前に、設計の前提が実際に成立するか確かめるためのスクリプトです。

## 検証済みの内容

`pgvector-design.sql` は、要件定義書の[類似度検索の実装方式](../docs/knowledge-base-system-requirements.md)が実際に動くかを確認します。

| # | 確認すること | 結果 |
|---|---|---|
| 1 | 次元数の異なるベクトルを同一カラムに格納できるか | 成立（768次元と1536次元が同居） |
| 2 | テナント別パーティション + 次元別部分インデックスで HNSW が使われるか | 成立（`Index Scan using idx_t1_768`） |
| 3 | キャスト式が揃わないと全件走査に落ちるか | 実証（4000行を `Seq Scan`、エラーは出ない） |
| 4 | `ef_search` がオーバーフェッチ件数の上限になるか | 実証（既定40では `LIMIT 100` でも40件）。ただし**発現はデータ分布に依存**（下記） |
| 5 | 行数が少ないとインデックスが使われないのは正常か | 確認（`enable_seqscan = off` で使われる） |

### `ef_search` の上限について補足

この上限は常に現れるわけではありません。実測した条件は次のとおりです。

| ベクトルの分布 | 行数 | HNSW | `ef_search=40` で `LIMIT 100` を要求した結果 |
|---|---|---|---|
| 散らばっている | 4000 | 使用 | **40件**（上限が効く） |
| ほぼ同一 | 4000 | 使用 | 100件（効かない） |
| 散らばっている | 2000 | 未使用 | 100件（Sort 経路のため無関係） |

**上限に達しない場合があるだけで、`ef_search` を超える件数を当てにはできません。** 必ず「オーバーフェッチ件数 ≤ `ef_search`」に設定してください。

この挙動は `tests/test_ingest_integration.py::test_ef_search_is_applied_when_index_is_used` で回帰テスト化してあります。上限が確実に現れる条件（4000行・分散したベクトル）を使っているため、`ef_search` の設定を外すとテストが失敗します。

検証環境: PostgreSQL 17 + pgvector 0.8.5

## 実行方法

### 1. PostgreSQL + pgvector を起動する

```bash
docker run -d --name kb-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=knowledge -p 55432:5432 pgvector/pgvector:pg17
```

拡張を有効化します。

```bash
docker exec kb-pg psql -U postgres -d knowledge -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 2. 検証スクリプトを実行する

```bash
docker cp verify/pgvector-design.sql kb-pg:/tmp/ && docker exec kb-pg psql -U postgres -d knowledge -f /tmp/pgvector-design.sql
```

### 3. 後片付け

```bash
docker rm -f kb-pg
```

## 開発環境について

この環境（WSL2 / Ubuntu 20.04）では、実装に必要なものが標準では揃いません。

| 項目 | 標準状態 | 対応 |
|---|---|---|
| Python | 3.8.10（markitdown は 3.10+ が必要） | `uv` で 3.12 を導入 |
| pip | 無し | `uv` が代替 |
| sudo | パスワードが必要で非対話実行が不可 | `~/.local/bin` へのユーザーインストールで回避 |
| PostgreSQL | 無し | Docker Desktop（WSL統合を有効化） |

`uv` の導入:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 の導入:

```bash
uv python install 3.12
```

markitdown が動くことは実ファイル（xlsx）で確認済みです。日本語を含む表が Markdown テーブルに変換されます。

> **Docker Desktop が起動しているのに `docker` が使えない場合**は、`wsl -l -v` で `docker-desktop` ディストロが `Running` か確認してください。`Stopped` のままなら、Docker Desktop の Settings → Resources → WSL Integration でトグルをオフ→Apply→オン→Apply し直すと復旧します。
