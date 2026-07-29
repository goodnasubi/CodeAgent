"""Re:lation（株式会社インゲージ）をバックエンドとする実装。

https://developer.ingage.jp/

Re:lation は問い合わせ対応の共有メールボックスであり、Issue トラッカーでは
ない。そのため他のバックエンドと比べて出来ることに差がある。

| 機能 | 可否 |
|---|---|
| 知識の登録・追記 | 可（応対メモ = record として作る） |
| ラベル | 可（ただし **ID 指定**。名前からの解決が要る） |
| 差分更新 | 可（`last_updated_since`） |
| キーワード検索 | **不可**（API が持たない） |
| 知識どうしのつながり | **不可**（チケット間を関連付ける API が無い） |

キーワード検索とつながりが使えないため、ハイブリッド検索は類似度検索の
1 本だけで動く。類似度検索は pgvector 側で完結するので、知識ベースとして
の主機能は成立する。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

import httpx

from .base import (
    Knowledge,
    KnowledgeBaseError,
    KnowledgeNotFound,
    Relation,
    UnsupportedOperation,
)

_PER_PAGE = 50  # API の上限
_MAX_PAGES = 50

# status_cds を省いたときの既定が不明なので、取り込みでは明示的に全部を渡す。
# 「対応完了」こそ知識として欲しい対象であり、取りこぼすと知識ベースとして
# 成立しないため（Redmine の status_id=* と同じ理由）。
ALL_STATUS_CDS = ("open", "ongoing", "closed", "unwanted")
"""trash / spam は知識ではないので除く。"""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class RelationKnowledgeBase:
    """Re:lation のチケットを知識として扱う。"""

    supports_keyword_search = False
    """API にキーワード検索が無い（公式ドキュメントに明記）。"""

    supports_relations = False
    """チケット同士を関連付ける API が無い。"""

    def __init__(
        self,
        *,
        access_token: str,
        subdomain: str,
        message_box_id: int | str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not subdomain:
            raise ValueError("subdomain は必須")
        if message_box_id in (None, ""):
            raise ValueError("message_box_id は必須")
        self._base = (
            f"https://{subdomain}.relationapp.jp/api/v2/{message_box_id}"
        )
        self._label_cache: dict[str, int] | None = None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise KnowledgeBaseError(f"Re:lation への接続に失敗しました: {exc}") from exc

        if response.status_code == 404:
            raise KnowledgeNotFound(f"見つかりません: {url}")
        if response.status_code == 401:
            raise KnowledgeBaseError("Re:lation のアクセストークンが拒否されました")
        if response.status_code == 429:
            # 60 リクエスト/分と厳しいので、いつ回復するかを添えて上げる
            reset = response.headers.get("X-RateLimit-Reset", "?")
            raise KnowledgeBaseError(
                f"Re:lation API のレート制限に達しました（reset={reset}）"
            )
        if response.status_code >= 400:
            raise KnowledgeBaseError(
                f"Re:lation API がエラーを返しました {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response

    # ---------------------------------------------------------------- labels

    def _labels(self) -> dict[str, int]:
        """ラベル名 → label_id の対応を引く（1 回だけ）。

        Re:lation はラベルを ID で指定するため、名前で扱う共通インター
        フェースとの間に変換が要る。
        """
        if self._label_cache is None:
            resp = self._request("GET", "/labels")
            self._label_cache = {
                item["name"]: int(item["label_id"])
                for item in resp.json()
                if item.get("name") and item.get("label_id") is not None
            }
        return self._label_cache

    def _label_ids(self, names: Sequence[str]) -> list[int]:
        table = self._labels()
        missing = [n for n in names if n not in table]
        if missing:
            raise KnowledgeBaseError(
                f"Re:lation に存在しないラベルです: {', '.join(missing)}"
            )
        return [table[n] for n in names]

    # --------------------------------------------------------------- mapping

    def _to_knowledge(self, payload: dict[str, Any]) -> Knowledge:
        """チケットを知識に写す。

        Re:lation のチケットは「やり取りの束」なので、最初のメッセージを
        本文、残りのメッセージと社内コメントを追記として扱う。こうすると
        combined_text にやり取り全体が入る。
        """
        ticket_id = payload.get("ticket_id")
        messages = payload.get("messages") or []
        bodies = [(m.get("body") or "").strip() for m in messages]
        bodies = [b for b in bodies if b]

        comments = [
            (c.get("comment") or "").strip()
            for c in payload.get("comments") or []
            if (c.get("comment") or "").strip()
        ]

        label_names = tuple(
            lbl["name"] if isinstance(lbl, dict) else str(lbl)
            for lbl in payload.get("labels") or ()
        )

        return Knowledge(
            id=str(ticket_id),
            title=payload.get("title") or "",
            body=bodies[0] if bodies else "",
            url=f"{self._base}/tickets/{ticket_id}",
            labels=label_names,
            updated_at=_parse_time(payload.get("last_updated_at")),
            comments=tuple([*bodies[1:], *comments]),
        )

    # ------------------------------------------------------------ operations

    def create(self, *, title: str, body: str, labels: Sequence[str] = ()) -> Knowledge:
        """応対メモとして新しいチケットを作る。

        Re:lation にはチケットを直接作る API が無い。`ticket_id` を省いて
        record を作ると、そのメモを含む新規チケットが生まれる。
        """
        if not title.strip():
            raise ValueError("title は必須")
        resp = self._request(
            "POST",
            "/records",
            json={
                "subject": title,
                "body": body,
                # 未来の時刻は受け付けられないため現在時刻を使う
                "operated_at": datetime.now(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration": 0,
            },
        )
        ticket_id = resp.json().get("ticket_id")
        if ticket_id is None:
            raise KnowledgeBaseError("Re:lation が ticket_id を返しませんでした")
        if labels:
            self.add_labels(str(ticket_id), labels)
        return self.get(str(ticket_id))

    def get(self, knowledge_id: str) -> Knowledge:
        resp = self._request("GET", f"/tickets/{knowledge_id}")
        return self._to_knowledge(resp.json())

    def append(self, knowledge_id: str, text: str) -> None:
        """応対メモとして追記する。既存メッセージは書き換えない。"""
        if not text.strip():
            raise ValueError("空のテキストは追記できない")
        self._request(
            "POST",
            "/records",
            json={
                "ticket_id": int(knowledge_id),
                "subject": "追記",
                "body": text,
                "operated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration": 0,
            },
        )

    def add_labels(self, knowledge_id: str, labels: Sequence[str]) -> None:
        """ラベルを追加する。

        `label_ids` は**置き換え**なので、既存を読んでから和集合を書き戻す。
        """
        if not labels:
            return
        current = self.get(knowledge_id).labels
        merged = list(dict.fromkeys([*current, *labels]))
        self._request(
            "PUT",
            f"/tickets/{knowledge_id}",
            json={"label_ids": self._label_ids(merged)},
        )

    def updated_since(self, since: datetime) -> Iterator[Knowledge]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for page in range(1, _MAX_PAGES + 1):
            resp = self._request(
                "POST",
                "/tickets/search",
                json={
                    "last_updated_since": stamp,
                    "status_cds": list(ALL_STATUS_CDS),
                    "per_page": _PER_PAGE,
                    "page": page,
                },
            )
            batch = resp.json()
            if not batch:
                return
            for item in batch:
                # 検索結果は要約なので、本文・コメントは個別に引く
                try:
                    yield self.get(str(item["ticket_id"]))
                except KnowledgeNotFound:
                    continue
            if len(batch) < _PER_PAGE:
                return

    def search(self, query: str, *, limit: int = 10) -> list[Knowledge]:
        """Re:lation にキーワード検索は無い。

        黙って空を返すと「ヒット 0 件」と区別できず、検索が弱くなったことに
        気づけない。呼び出し側は `supports_keyword_search` を見て飛ばすこと。
        """
        raise UnsupportedOperation(
            "Re:lation の API はキーワード検索に対応していません"
            "（supports_keyword_search で判定してください）"
        )

    def relations(self, knowledge_id: str) -> list[Relation]:
        """Re:lation にチケット間を関連付ける API は無い。

        つながりが「無い」ことは正しい状態なので、例外ではなく空を返す。
        グラフ展開は結果を変えないだけで、検索自体は成立する。
        """
        return []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RelationKnowledgeBase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
