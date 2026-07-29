"""Redmine をバックエンドとする実装。

Redmine は self-hosted 前提の製品なので、API のベース URL は必須。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

import httpx

from .base import Knowledge, KnowledgeBaseError, KnowledgeNotFound, Relation

_PER_PAGE = 100
_MAX_PAGES = 50

DEFAULT_LABEL_FIELD = "Labels"
"""ラベルとして使うカスタムフィールドの名前（下記の注意を参照）。"""


class LabelFieldMissing(KnowledgeBaseError):
    """ラベル用のカスタムフィールドが未設定。"""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RedmineKnowledgeBase:
    """Redmine のチケットを知識として扱う。

    **ラベルについて**: Redmine にラベル機能は無い。カテゴリは単一値、
    トラッカーは種別であり、いずれも複数のラベルを付ける用途に合わない。
    そのため**複数選択のカスタムフィールド**をラベルとして使う。
    Redmine 側での一度きりの設定が必要:

        管理 → カスタムフィールド → 新しいカスタムフィールド
          形式: リスト / 複数選択: ON / 対象: 全トラッカー・全プロジェクト

    未設定でも読み書き以外の機能は動く。ラベルの取得は空になり、
    付与は LabelFieldMissing を送出して設定漏れを気づけるようにする。
    """

    supports_keyword_search = True
    supports_relations = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        project: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        fetch_comments: bool = True,
        label_field: str = DEFAULT_LABEL_FIELD,
    ) -> None:
        if not base_url:
            raise ValueError("base_url は必須（Redmine は self-hosted のため）")
        if not project:
            raise ValueError("project は識別子または数値 ID で指定する")
        self._base = base_url.rstrip("/")
        self._project = project
        self._label_field = label_field
        self._fetch_comments = fetch_comments
        self._label_field_id: int | None = None
        self._label_field_looked_up = False
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "X-Redmine-API-Key": api_key,
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeBaseError(f"Redmine への接続に失敗しました: {exc}") from exc

        if response.status_code == 404:
            raise KnowledgeNotFound(f"見つかりません: {url}")
        if response.status_code == 401:
            raise KnowledgeBaseError("Redmine の API キーが拒否されました")
        if response.status_code == 403:
            raise KnowledgeBaseError(
                "Redmine で権限がありません（REST API が無効の可能性）"
            )
        if response.status_code >= 400:
            raise KnowledgeBaseError(
                f"Redmine API がエラーを返しました {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    def _paged(self, path: str, key: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """offset / limit 方式のページングをたどる。"""
        query = dict(params or {})
        query["limit"] = _PER_PAGE
        offset = 0
        for _ in range(_MAX_PAGES):
            resp = self._request("GET", path, params={**query, "offset": offset})
            payload = resp.json()
            batch = payload.get(key) or []
            if not batch:
                return
            yield from batch
            offset += len(batch)
            if offset >= int(payload.get("total_count", 0)):
                return

    # ----------------------------------------------------------------- labels

    def _label_field_ident(self) -> int | None:
        """ラベル用カスタムフィールドの ID を引く（1 回だけ）。"""
        if self._label_field_looked_up:
            return self._label_field_id
        self._label_field_looked_up = True
        try:
            resp = self._request("GET", "/custom_fields.json")
        except KnowledgeBaseError:
            # 管理者権限が無いと引けない。ラベル無しとして扱う
            return None
        for field in resp.json().get("custom_fields", []):
            if field.get("name") == self._label_field:
                self._label_field_id = int(field["id"])
                break
        return self._label_field_id

    @staticmethod
    def _labels_from(payload: dict[str, Any], field_name: str) -> tuple[str, ...]:
        for field in payload.get("custom_fields") or ():
            if field.get("name") != field_name:
                continue
            value = field.get("value")
            if isinstance(value, list):
                return tuple(v for v in value if v)
            if value:
                return (str(value),)
        return ()

    # ---------------------------------------------------------------- mapping

    def _comments(self, payload: dict[str, Any]) -> tuple[str, ...]:
        """journals から人が書いた注記だけを取り出す。

        journals には「優先度を変更した」のような**属性変更だけの項目**も
        含まれ、その場合 notes は空になる。除外しないと操作履歴が
        知識として embedding されてしまう。
        """
        if not self._fetch_comments:
            return ()
        out = []
        for journal in payload.get("journals") or ():
            note = (journal.get("notes") or "").strip()
            if note:
                out.append(note)
        return tuple(out)

    def _to_knowledge(self, payload: dict[str, Any]) -> Knowledge:
        issue_id = payload.get("id")
        return Knowledge(
            id=str(issue_id),
            title=payload.get("subject") or "",
            body=payload.get("description") or "",
            url=f"{self._base}/issues/{issue_id}",
            labels=self._labels_from(payload, self._label_field),
            updated_at=_parse_time(payload.get("updated_on")),
            comments=self._comments(payload),
        )

    # ------------------------------------------------------------ operations

    def create(self, *, title: str, body: str, labels: Sequence[str] = ()) -> Knowledge:
        issue: dict[str, Any] = {
            "project_id": self._project,
            "subject": title,
            "description": body,
        }
        if labels:
            field_id = self._label_field_ident()
            if field_id is None:
                raise LabelFieldMissing(
                    f"ラベル用のカスタムフィールド '{self._label_field}' が"
                    " Redmine 側に存在しません"
                )
            issue["custom_fields"] = [{"id": field_id, "value": list(labels)}]
        resp = self._request("POST", "/issues.json", json={"issue": issue})
        return self._to_knowledge(resp.json()["issue"])

    def get(self, knowledge_id: str) -> Knowledge:
        resp = self._request(
            "GET", f"/issues/{knowledge_id}.json", params={"include": "journals"}
        )
        return self._to_knowledge(resp.json()["issue"])

    def append(self, knowledge_id: str, text: str) -> None:
        """注記として追記する。説明欄は書き換えない。"""
        if not text.strip():
            raise ValueError("空のテキストは追記できない")
        self._request(
            "PUT", f"/issues/{knowledge_id}.json", json={"issue": {"notes": text}}
        )

    def add_labels(self, knowledge_id: str, labels: Sequence[str]) -> None:
        """ラベルを追加する。

        カスタムフィールドは**置き換え**なので、既存の値を読んでから
        和集合を書き戻す。
        """
        if not labels:
            return
        field_id = self._label_field_ident()
        if field_id is None:
            raise LabelFieldMissing(
                f"ラベル用のカスタムフィールド '{self._label_field}' が"
                " Redmine 側に存在しません"
            )
        current = self.get(knowledge_id).labels
        merged = list(dict.fromkeys([*current, *labels]))
        self._request(
            "PUT",
            f"/issues/{knowledge_id}.json",
            json={"issue": {"custom_fields": [{"id": field_id, "value": merged}]}},
        )

    def updated_since(self, since: datetime) -> Iterator[Knowledge]:
        """指定時刻以降に更新されたチケットを返す。

        `status_id=*` を付けないと**未完了のものしか返らない**。解決済みの
        知識こそ検索したい対象なので、これが抜けると取りこぼす。
        """
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "project_id": self._project,
            "status_id": "*",
            "updated_on": f">={stamp}",
            "sort": "updated_on:desc",
        }
        for item in self._paged("/issues.json", "issues", params):
            # 一覧には journals が含まれないため、注記が要る場合は個別に引く
            yield self.get(str(item["id"])) if self._fetch_comments else self._to_knowledge(item)

    def search(self, query: str, *, limit: int = 10) -> list[Knowledge]:
        resp = self._request(
            "GET",
            "/search.json",
            params={
                "q": query,
                "issues": 1,
                "titles_only": 0,
                "limit": min(limit, _PER_PAGE),
                "scope": "all",
                "project_id": self._project,
            },
        )
        results = resp.json().get("results", [])[:limit]
        # 検索結果は要約しか返さないため、本文・ラベルは個別に引く
        out: list[Knowledge] = []
        for hit in results:
            try:
                out.append(self.get(str(hit["id"])))
            except KnowledgeNotFound:
                continue
        return out

    def relations(self, knowledge_id: str) -> list[Relation]:
        """関連チケットを返す。

        Redmine は関連を第一級機能として持ち、`relation_type` は
        relates / duplicates / blocks / precedes / copied_to などが入る。
        プランによる制限も無い。

        関連は 1 レコードで双方向を表すため、自分が issue_id 側か
        issue_to_id 側かを見て相手を決める。
        """
        found: dict[str, Relation] = {}
        me = str(knowledge_id)
        resp = self._request("GET", f"/issues/{knowledge_id}/relations.json")
        for item in resp.json().get("relations", []):
            a, b = str(item.get("issue_id")), str(item.get("issue_to_id"))
            other = b if a == me else a
            if other in (me, "None", ""):
                continue
            found[other] = Relation(
                from_id=me, to_id=other, kind=item.get("relation_type") or "relates"
            )
        return list(found.values())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RedmineKnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
