"""GitLab Issues をバックエンドとする実装。

self-hosted GitLab にも向けられるよう、API のベース URL を設定可能にする。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence
from urllib.parse import quote

import httpx

from .base import Knowledge, KnowledgeBaseError, KnowledgeNotFound, Relation

_API = "https://gitlab.com/api/v4"
_PER_PAGE = 100
_MAX_PAGES = 50

# システムノートに残る参照の記録。`#12` の前に文字が続く場合
# （group/project#12 のような他プロジェクト参照）は拾わない。
_MENTIONED = re.compile(r"(?:^|\s)mentioned in issue #(\d+)\b")
_MARKED_RELATED = re.compile(r"(?:^|\s)marked this issue as related to #(\d+)\b")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitLabKnowledgeBase:
    """GitLab の Issue を知識として扱う。

    GitLab には ID が 2 つある。`id` はインスタンス全体で一意、`iid` は
    プロジェクト内の連番で、画面や `#123` 表記に出るのはこちら。利用者が
    目にする番号と揃えるため、知識の ID には **iid** を使う。
    """

    supports_keyword_search = True
    supports_relations = True

    def __init__(
        self,
        *,
        token: str,
        project: str,
        api_base: str = _API,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        fetch_comments: bool = True,
    ) -> None:
        if not project:
            raise ValueError("project は 'group/name' または数値 ID で指定する")
        # "group/sub/name" のようなネストも URL エンコードして 1 セグメントにする
        self._project = quote(str(project), safe="")
        self._api = api_base.rstrip("/")
        self._fetch_comments = fetch_comments
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"PRIVATE-TOKEN": token},
        )

    # ------------------------------------------------------------------ HTTP

    def _request(
        self, method: str, path: str, *, tolerate: tuple[int, ...] = (), **kwargs: Any
    ) -> httpx.Response | None:
        """API を叩く。

        `tolerate` に挙げたステータスは例外にせず None を返す。バージョンや
        プランによって存在しないエンドポイントを、機能なしとして扱うために使う。
        """
        url = path if path.startswith("http") else f"{self._api}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeBaseError(f"GitLab への接続に失敗しました: {exc}") from exc

        if response.status_code in tolerate:
            return None
        if response.status_code == 404:
            raise KnowledgeNotFound(f"見つかりません: {url}")
        if response.status_code == 429:
            raise KnowledgeBaseError("GitLab API のレート制限に達しました")
        if response.status_code >= 400:
            raise KnowledgeBaseError(
                f"GitLab API がエラーを返しました {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    def _paged(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """GitLab のページングをたどる。

        Link ヘッダのほか `X-Next-Page` ヘッダでも次ページが示される。
        空文字なら終端。
        """
        query = dict(params or {})
        query.setdefault("per_page", _PER_PAGE)
        page = 1
        for _ in range(_MAX_PAGES):
            resp = self._request("GET", path, params={**query, "page": page})
            assert resp is not None  # tolerate を渡していないので必ず返る
            batch = resp.json()
            if not batch:
                return
            yield from batch
            nxt = resp.headers.get("X-Next-Page", "")
            if not nxt:
                return
            page = int(nxt)

    def _comments(self, iid: int | str) -> tuple[str, ...]:
        """ノートを集める。

        GitLab のノートには「ラベルを追加した」といった**システムノート**が
        混ざる。除外しないと操作履歴が知識として embedding されてしまう。
        """
        if not self._fetch_comments:
            return ()
        out: list[str] = []
        for note in self._paged(f"/projects/{self._project}/issues/{iid}/notes"):
            if note.get("system"):
                continue
            body = note.get("body") or ""
            if body.strip():
                out.append(body)
        return tuple(out)

    def _to_knowledge(self, payload: dict[str, Any], *, with_comments: bool) -> Knowledge:
        iid = payload.get("iid")
        return Knowledge(
            id=str(iid),
            title=payload.get("title") or "",
            body=payload.get("description") or "",
            url=payload.get("web_url") or "",
            labels=tuple(payload.get("labels") or ()),
            updated_at=_parse_time(payload.get("updated_at")),
            comments=self._comments(iid) if with_comments else (),
        )

    # ------------------------------------------------------------ operations

    def create(self, *, title: str, body: str, labels: Sequence[str] = ()) -> Knowledge:
        payload: dict[str, Any] = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        resp = self._request("POST", f"/projects/{self._project}/issues", json=payload)
        assert resp is not None
        return self._to_knowledge(resp.json(), with_comments=False)

    def get(self, knowledge_id: str) -> Knowledge:
        resp = self._request("GET", f"/projects/{self._project}/issues/{knowledge_id}")
        assert resp is not None
        return self._to_knowledge(resp.json(), with_comments=True)

    def append(self, knowledge_id: str, text: str) -> None:
        """ノートとして追記する。本文は書き換えない。"""
        if not text.strip():
            raise ValueError("空のテキストは追記できない")
        self._request(
            "POST",
            f"/projects/{self._project}/issues/{knowledge_id}/notes",
            json={"body": text},
        )

    def add_labels(self, knowledge_id: str, labels: Sequence[str]) -> None:
        """ラベルを追加する。

        `labels` を渡すと置き換えになるため、既存を消さない `add_labels` を使う。
        """
        if not labels:
            return
        self._request(
            "PUT",
            f"/projects/{self._project}/issues/{knowledge_id}",
            json={"add_labels": ",".join(labels)},
        )

    def updated_since(self, since: datetime) -> Iterator[Knowledge]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        params = {
            "updated_after": since.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "scope": "all",
            "order_by": "updated_at",
            "sort": "desc",
        }
        for item in self._paged(f"/projects/{self._project}/issues", params):
            yield self._to_knowledge(item, with_comments=True)

    def search(self, query: str, *, limit: int = 10) -> list[Knowledge]:
        resp = self._request(
            "GET",
            f"/projects/{self._project}/issues",
            params={"search": query, "scope": "all", "per_page": min(limit, _PER_PAGE)},
        )
        assert resp is not None
        return [
            self._to_knowledge(item, with_comments=False) for item in resp.json()[:limit]
        ]

    def relations(self, knowledge_id: str) -> list[Relation]:
        """つながりを返す。2 つの経路を併用する。

        **① 関連イシュー API**（`/links`）
        GitLab の第一級機能で `link_type`（relates_to / blocks /
        is_blocked_by）まで取れる。ただし **Premium 以上の機能**であり、
        Free 版や古いバージョンでは使えない。使えない場合は黙って諦める。

        **② システムノートの参照記録**
        本文やノートに `#123` と書くと、GitLab は参照された側に
        `mentioned in issue #123` というシステムノートを自動で残す。
        こちらは古くからある機能でプランにも依存しないため、①が使えない
        環境でもつながりを拾える。

        ②は GitHub の cross-referenced と同じく**被参照側にしか残らない**が、
        取り込みは全知識を走査し、探索側は辺を両向きに辿るため欠落しない。
        """
        found: dict[str, Relation] = {}

        # ① 関連イシュー API（無い環境では 403/404 が返るので機能なしとして扱う）
        probe = self._request(
            "GET",
            f"/projects/{self._project}/issues/{knowledge_id}/links",
            params={"per_page": _PER_PAGE},
            tolerate=(403, 404),
        )
        if probe is not None:
            for item in probe.json():
                other = item.get("iid")
                if other is None or str(other) == str(knowledge_id):
                    continue
                found[str(other)] = Relation(
                    from_id=str(knowledge_id),
                    to_id=str(other),
                    kind=item.get("link_type") or "relates_to",
                )

        # ② システムノートから参照を拾う
        for other in self._mentioned_in(knowledge_id):
            found.setdefault(
                other,
                Relation(from_id=str(knowledge_id), to_id=other, kind="mentioned"),
            )

        return list(found.values())

    def _mentioned_in(self, knowledge_id: str) -> set[str]:
        """システムノートから、同一プロジェクト内の参照元 iid を集める。

        `group/project#12` のような他プロジェクト参照は拾わない。知識 ID が
        プロジェクト内の連番であり、他プロジェクトの番号と混ざるため。
        """
        out: set[str] = set()
        for note in self._paged(f"/projects/{self._project}/issues/{knowledge_id}/notes"):
            if not note.get("system"):
                continue
            body = note.get("body") or ""
            for pattern in (_MENTIONED, _MARKED_RELATED):
                for match in pattern.finditer(body):
                    other = match.group(1)
                    if other != str(knowledge_id):
                        out.add(other)
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitLabKnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
