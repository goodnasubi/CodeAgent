"""GitHub Issues をバックエンドとする実装。

GitHub Enterprise Server にも向けられるよう、API のベース URL を設定可能にする。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

import httpx

from .base import Knowledge, KnowledgeBaseError, KnowledgeNotFound, Relation

_API = "https://api.github.com"
_PER_PAGE = 100
# ページングの暴走を防ぐ上限。1 回のポーリングでこれを超える更新があるのは
# 初回取り込みなど例外的な状況で、その場合は since を刻んで複数回に分ける。
_MAX_PAGES = 50


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubKnowledgeBase:
    """GitHub の Issue を知識として扱う。"""

    supports_keyword_search = True
    supports_relations = True

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        api_base: str = _API,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        fetch_comments: bool = True,
    ) -> None:
        if "/" not in repository:
            raise ValueError("repository は 'owner/name' の形式で指定する")
        self._repo = repository
        self._api = api_base.rstrip("/")
        self._fetch_comments = fetch_comments
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._api}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeBaseError(f"GitHub への接続に失敗しました: {exc}") from exc

        if response.status_code == 404:
            raise KnowledgeNotFound(f"見つかりません: {url}")
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "?")
            raise KnowledgeBaseError(
                f"GitHub API のレート制限に達しました（reset={reset}）"
            )
        if response.status_code >= 400:
            raise KnowledgeBaseError(
                f"GitHub API がエラーを返しました {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    def _comments(self, number: int) -> tuple[str, ...]:
        if not self._fetch_comments:
            return ()
        out: list[str] = []
        url = f"{self._api}/repos/{self._repo}/issues/{number}/comments"
        params: dict[str, Any] | None = {"per_page": _PER_PAGE}
        for _ in range(_MAX_PAGES):
            resp = self._request("GET", url, params=params)
            out.extend(c.get("body") or "" for c in resp.json())
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None
        return tuple(out)

    def _to_knowledge(self, payload: dict[str, Any], *, with_comments: bool) -> Knowledge:
        number = payload["number"]
        return Knowledge(
            id=str(number),
            title=payload.get("title") or "",
            body=payload.get("body") or "",
            url=payload.get("html_url") or "",
            labels=tuple(
                lbl["name"] if isinstance(lbl, dict) else str(lbl)
                for lbl in payload.get("labels") or ()
            ),
            updated_at=_parse_time(payload.get("updated_at")),
            comments=self._comments(number)
            if with_comments and payload.get("comments")
            else (),
        )

    # ------------------------------------------------------------ operations

    def create(
        self, *, title: str, body: str, labels: Sequence[str] = ()
    ) -> Knowledge:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        resp = self._request("POST", f"/repos/{self._repo}/issues", json=payload)
        return self._to_knowledge(resp.json(), with_comments=False)

    def get(self, knowledge_id: str) -> Knowledge:
        resp = self._request("GET", f"/repos/{self._repo}/issues/{knowledge_id}")
        payload = resp.json()
        if "pull_request" in payload:
            raise KnowledgeNotFound(f"#{knowledge_id} は Pull Request で知識ではない")
        return self._to_knowledge(payload, with_comments=True)

    def append(self, knowledge_id: str, text: str) -> None:
        """コメントとして追記する。

        本文を書き換えないのは、同時編集で他人の記述を失う危険を避けるため。
        """
        if not text.strip():
            raise ValueError("空のテキストは追記できない")
        self._request(
            "POST",
            f"/repos/{self._repo}/issues/{knowledge_id}/comments",
            json={"body": text},
        )

    def add_labels(self, knowledge_id: str, labels: Sequence[str]) -> None:
        if not labels:
            return
        self._request(
            "POST",
            f"/repos/{self._repo}/issues/{knowledge_id}/labels",
            json={"labels": list(labels)},
        )

    def updated_since(self, since: datetime) -> Iterator[Knowledge]:
        """指定時刻以降に更新された Issue を新しい順に返す。

        GitHub の issues エンドポイントは **Pull Request も返す**ため、
        除外しないと PR が知識として取り込まれてしまう。
        """
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        url = f"{self._api}/repos/{self._repo}/issues"
        params: dict[str, Any] | None = {
            "since": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "state": "all",
            "per_page": _PER_PAGE,
            "sort": "updated",
            "direction": "desc",
        }
        for _ in range(_MAX_PAGES):
            resp = self._request("GET", url, params=params)
            batch = resp.json()
            if not batch:
                return
            for item in batch:
                if "pull_request" in item:
                    continue
                yield self._to_knowledge(item, with_comments=True)
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                return
            url, params = nxt, None

    def search(self, query: str, *, limit: int = 10) -> list[Knowledge]:
        """キーワード検索。

        スコアは返さない。バックエンドごとに関連度の定義が異なり比較できず、
        マージは順位のみを使う RRF で行うため。
        """
        resp = self._request(
            "GET",
            "/search/issues",
            params={
                "q": f"repo:{self._repo} is:issue {query}",
                "per_page": min(limit, _PER_PAGE),
            },
        )
        items = resp.json().get("items", [])
        return [self._to_knowledge(i, with_comments=False) for i in items[:limit]]

    def relations(self, knowledge_id: str) -> list[Relation]:
        """timeline から参照を集める。

        GitHub には「関連 Issue」という専用の項目がなく、本文やコメントに
        `#123` と書くことでつながりが生まれる。これは timeline の
        `cross-referenced` イベントとして現れる。

        **向きに注意**: このイベントは「他から参照された」側にしか立たない。
        A の本文に `#B` と書いた場合、イベントが付くのは B の timeline で、
        A からは見えない。したがってここが返すのは**被参照のみ**で、
        自分が書いた参照は含まれない。

        それでも関係グラフは欠けない。取り込みは全知識を走査するので
        B の同期時に A とのつながりが記録され、探索側（neighbours）は
        辺を両向きに辿るため、A からも B に到達できる。

        PR からの参照も同じイベント種別で来るため、除外しないと PR が
        知識として関係グラフに混入する。
        """
        url = f"{self._api}/repos/{self._repo}/issues/{knowledge_id}/timeline"
        params: dict[str, Any] | None = {"per_page": _PER_PAGE}
        found: dict[str, Relation] = {}

        for _ in range(_MAX_PAGES):
            resp = self._request("GET", url, params=params)
            for event in resp.json():
                if event.get("event") != "cross-referenced":
                    continue
                source = (event.get("source") or {}).get("issue") or {}
                number = source.get("number")
                if number is None or "pull_request" in source:
                    continue
                other = str(number)
                if other != str(knowledge_id):
                    found[other] = Relation(
                        from_id=str(knowledge_id), to_id=other, kind="references"
                    )
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None

        return list(found.values())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubKnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
