"""知識ベースバックエンドの共通インターフェース。

GitLab / GitHub / Redmine を同一の口で扱う。テナントごとに 1 つだけ選択され、
複数を同時に問い合わせることはない。

知識の正はこちら側にあり、pgvector はここから導出された検索用インデックス。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Protocol, Sequence, runtime_checkable

# 本文とコメントを 1 つのテキストに束ねるときの区切り。
# 埋め込み時に「どこからがコメントか」を模型が手掛かりにできるようにする。
COMMENT_MARKER = "[コメント {n}]"
ATTACHMENT_MARKER = "[添付 {name}]"


@dataclass(frozen=True)
class Relation:
    """知識どうしのつながり。

    KB 側が持っている関係をそのまま写す。ラベルと同じく KB ネイティブの
    情報であり、こちらは派生データとして保持する（捨てて作り直せる）。
    """

    from_id: str
    to_id: str
    kind: str = "references"
    """バックエンドが返す関係の種類。GitHub は相互参照のみ、Redmine は
    relates / duplicates / blocks / precedes などを持つ。"""


@dataclass(frozen=True)
class Knowledge:
    """KB 上の 1 件の知識（GitLab/GitHub の Issue、Redmine のチケット）。"""

    id: str
    title: str
    body: str
    url: str
    labels: tuple[str, ...] = ()
    updated_at: datetime | None = None
    comments: tuple[str, ...] = ()
    attachments: tuple[tuple[str, str], ...] = field(default=())
    """(ファイル名, 抽出済みテキスト) の並び。OCR/変換済みのものだけを持つ。"""

    def combined_text(self) -> str:
        """embedding に渡すテキスト。

        タイトル・本文・コメント・添付の抽出結果を、区切りマーカー付きで
        1 つにまとめる。元の設計メモの combined_text と同じ考え方。
        """
        parts: list[str] = []
        if self.title:
            parts.append(self.title)
        if self.body:
            parts.append(self.body)
        for i, comment in enumerate(self.comments, start=1):
            if comment.strip():
                parts.append(f"{COMMENT_MARKER.format(n=i)}\n{comment}")
        for name, text in self.attachments:
            if text.strip():
                parts.append(f"{ATTACHMENT_MARKER.format(name=name)}\n{text}")
        return "\n\n".join(parts)


class KnowledgeBaseError(RuntimeError):
    """バックエンドとのやり取りに失敗した。"""


class KnowledgeNotFound(KnowledgeBaseError):
    pass


@runtime_checkable
class KnowledgeBase(Protocol):
    """バックエンドが満たすべき操作。

    追記はコメントとして行う。本文を書き換える方式は、同時編集で他人の
    記述を消す危険があり、また 3 バックエンドとも「コメント / 注記」を
    標準で持つため、こちらが移植性の面でも優れる。
    """

    def create(
        self, *, title: str, body: str, labels: Sequence[str] = ()
    ) -> Knowledge: ...

    def get(self, knowledge_id: str) -> Knowledge: ...

    def append(self, knowledge_id: str, text: str) -> None:
        """既存の知識に追記する（コメントとして追加）。"""

    def add_labels(self, knowledge_id: str, labels: Sequence[str]) -> None: ...

    def updated_since(self, since: datetime) -> Iterator[Knowledge]:
        """指定時刻以降に更新された知識を列挙する。cron ポーリングで使う。"""

    def search(self, query: str, *, limit: int = 10) -> list[Knowledge]:
        """キーワード検索。ハイブリッド検索のキーワード側。

        返すのは順位付きの並び。スコアは返さない（バックエンドごとに
        定義が異なり比較できないため、マージは RRF で順位のみを使う）。
        """

    def relations(self, knowledge_id: str) -> list[Relation]:
        """その知識がつながっている先を返す。

        人が張ったつながりなので、語も意味も一致しない知識に辿り着ける。
        類似度検索とキーワード検索のどちらも拾えない領域を埋める。
        """
