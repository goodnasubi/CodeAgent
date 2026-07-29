"""ラベル付与を起点とした通知。

トリガーはラベル。宛先は「ラベル → 通知先」の対応で決める。KB のアサイン先や
ウォッチャーには従わない（認証を最小構成にしたため、KB のユーザーと本システムの
アカウントを紐付ける土台が無い）。

配信先はアプリ内・Slack・メールの 3 つ。
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable, Protocol, Sequence
from uuid import UUID

import httpx

from .backends.base import Knowledge
from .db.notifications import NotificationRepository

logger = logging.getLogger(__name__)

CHANNEL_IN_APP = "in_app"
CHANNEL_SLACK = "slack"
CHANNEL_EMAIL = "email"


@dataclass(frozen=True)
class Notification:
    """配信する 1 件の通知。"""

    tenant_id: UUID
    knowledge: Knowledge
    label: str
    destination: str

    def summary(self) -> str:
        return f"[{self.label}] {self.knowledge.title}"


@dataclass
class DispatchResult:
    delivered: list[tuple[str, str]] = field(default_factory=list)
    """(チャネル, 宛先) の並び。"""
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    """(チャネル, 宛先, エラー内容) の並び。"""

    @property
    def ok(self) -> bool:
        return not self.failed


class Notifier(Protocol):
    channel: str

    def send(self, notification: Notification) -> None:
        """1 件配信する。失敗したら例外を送出する。"""


class InAppNotifier:
    """チャット画面に出す通知。DB の受信箱に積む。"""

    channel = CHANNEL_IN_APP

    def __init__(self, repository: NotificationRepository) -> None:
        self._repo = repository

    def send(self, notification: Notification) -> None:
        self._repo.record_app_notification(
            tenant_id=notification.tenant_id,
            kb_issue_id=notification.knowledge.id,
            label=notification.label,
            title=notification.knowledge.title,
            url=notification.knowledge.url or None,
        )


class SlackNotifier:
    """Incoming Webhook へ送る。宛先は Webhook URL。"""

    channel = CHANNEL_SLACK

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 10.0) -> None:
        self._client = client or httpx.Client(timeout=timeout)

    def send(self, notification: Notification) -> None:
        knowledge = notification.knowledge
        lines = [f"*{notification.summary()}*"]
        if knowledge.url:
            lines.append(knowledge.url)
        body = (knowledge.body or "").strip()
        if body:
            lines.append(body[:300] + ("…" if len(body) > 300 else ""))

        response = self._client.post(
            notification.destination, json={"text": "\n".join(lines)}
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Slack への送信に失敗しました {response.status_code}: {response.text[:100]}"
            )

    def close(self) -> None:
        self._client.close()


SmtpFactory = Callable[[], smtplib.SMTP]


class EmailNotifier:
    """メールで送る。宛先はメールアドレス。

    SMTP 接続の作り方は差し替えられるようにしてある（テストと、社内の
    SMTP 事情に合わせるため）。
    """

    channel = CHANNEL_EMAIL

    def __init__(self, *, sender: str, smtp_factory: SmtpFactory) -> None:
        self._sender = sender
        self._smtp_factory = smtp_factory

    def send(self, notification: Notification) -> None:
        knowledge = notification.knowledge
        message = EmailMessage()
        message["Subject"] = notification.summary()
        message["From"] = self._sender
        message["To"] = notification.destination

        parts = [f"ラベル「{notification.label}」が付与されました。", ""]
        if knowledge.title:
            parts.append(knowledge.title)
        if knowledge.url:
            parts.append(knowledge.url)
        if (knowledge.body or "").strip():
            parts += ["", knowledge.body.strip()]
        message.set_content("\n".join(parts))

        with self._smtp_factory() as smtp:
            smtp.send_message(message)


class NotificationDispatcher:
    """ラベルの差分を見て、必要な通知を配る。"""

    def __init__(
        self,
        *,
        repository: NotificationRepository,
        notifiers: Sequence[Notifier],
    ) -> None:
        self._repo = repository
        self._notifiers = {n.channel: n for n in notifiers}

    def new_labels(self, *, tenant_id: UUID, knowledge: Knowledge) -> tuple[str, ...]:
        """前回の同期以降に「付いた」ラベルを返す。

        初回取り込み（前回の記録が無い）では空を返す。既存の全ラベルを
        新規付与として扱うと、運用開始時に大量の通知が飛ぶため。
        """
        known = self._repo.known_labels(
            tenant_id=tenant_id, kb_issue_id=knowledge.id
        )
        if known is None:
            return ()
        return tuple(lbl for lbl in knowledge.labels if lbl not in known)

    def dispatch(self, *, tenant_id: UUID, knowledge: Knowledge) -> DispatchResult:
        """ラベルの差分を検知して配信し、状態を更新する。

        配信に失敗しても状態は更新する。更新しないと次のポーリングで
        同じ通知を延々と送り続けることになるため。失敗は結果に載せて
        呼び出し側が拾えるようにする。
        """
        result = DispatchResult()
        added = self.new_labels(tenant_id=tenant_id, knowledge=knowledge)

        for label in added:
            for rule in self._repo.rules_for(tenant_id=tenant_id, label=label):
                notifier = self._notifiers.get(rule.channel)
                if notifier is None:
                    result.failed.append(
                        (rule.channel, rule.destination, "未対応のチャネル")
                    )
                    continue
                notification = Notification(
                    tenant_id=tenant_id,
                    knowledge=knowledge,
                    label=label,
                    destination=rule.destination,
                )
                try:
                    notifier.send(notification)
                except Exception as exc:  # 1 件の失敗で他を止めない
                    logger.warning(
                        "通知の配信に失敗: channel=%s dest=%s label=%s: %s",
                        rule.channel,
                        rule.destination,
                        label,
                        exc,
                    )
                    result.failed.append((rule.channel, rule.destination, str(exc)))
                else:
                    result.delivered.append((rule.channel, rule.destination))

        self._repo.remember_labels(
            tenant_id=tenant_id, kb_issue_id=knowledge.id, labels=knowledge.labels
        )
        return result
