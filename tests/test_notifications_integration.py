"""通知の統合テスト。ラベル付与の検知から配信まで。"""

import smtplib
import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from kb.backends.base import Knowledge
from kb.db.notifications import NotificationRepository
from kb.notifications import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNEL_SLACK,
    EmailNotifier,
    InAppNotifier,
    NotificationDispatcher,
    SlackNotifier,
)

pytestmark = pytest.mark.integration


def knowledge(labels=(), *, kid="issue-1", title="ORA-01555 が発生する"):
    return Knowledge(
        id=kid,
        title=title,
        body="UNDO 表領域が不足している",
        url="https://example.test/issues/1",
        labels=tuple(labels),
    )


@pytest.fixture
def notif_repo(repo):
    """スキーマは ChunkRepository が作るので、それに相乗りする。"""
    return NotificationRepository(repo._conn)


@pytest.fixture
def clean(notif_repo, tenant_id):
    yield
    conn = notif_repo._conn
    for table in ("notification_rules", "knowledge_label_state", "app_notifications"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))


@pytest.fixture
def dispatcher(notif_repo, clean):
    return NotificationDispatcher(
        repository=notif_repo, notifiers=[InAppNotifier(notif_repo)]
    )


# ------------------------------------------------------------- 差分の検知


def test_first_sync_does_not_notify_existing_labels(dispatcher, notif_repo, tenant_id):
    """運用開始時に既存ラベル分の通知が大量に飛ばないこと。"""
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    result = dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert result.delivered == []
    assert notif_repo.unread(tenant_id=tenant_id) == []


def test_label_added_after_first_sync_notifies(dispatcher, notif_repo, tenant_id):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))  # 取り込み
    result = dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert result.delivered == [(CHANNEL_IN_APP, "")]
    inbox = notif_repo.unread(tenant_id=tenant_id)
    assert len(inbox) == 1
    assert inbox[0].label == "重大"
    assert inbox[0].kb_issue_id == "issue-1"


def test_same_label_is_not_notified_twice(dispatcher, notif_repo, tenant_id):
    """10 分ごとのポーリングで同じ通知を繰り返さないこと。"""
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert len(notif_repo.unread(tenant_id=tenant_id)) == 1


def test_removing_then_re_adding_a_label_notifies_again(
    dispatcher, notif_repo, tenant_id
):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))  # 外された
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert len(notif_repo.unread(tenant_id=tenant_id)) == 2


def test_label_without_a_rule_is_ignored(dispatcher, notif_repo, tenant_id):
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    result = dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["対応不要"]))

    assert result.delivered == []
    assert notif_repo.unread(tenant_id=tenant_id) == []


def test_only_the_newly_added_label_notifies(dispatcher, notif_repo, tenant_id):
    for label in ("重大", "DB"):
        notif_repo.add_rule(
            tenant_id=tenant_id, label=label, channel=CHANNEL_IN_APP, destination=""
        )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大", "DB"]))

    assert [n.label for n in notif_repo.unread(tenant_id=tenant_id)] == ["DB", "重大"]


# --------------------------------------------------------- 宛先の解決


def test_one_label_can_reach_several_destinations(notif_repo, tenant_id, clean):
    sent = []

    class Fake:
        channel = CHANNEL_SLACK

        def send(self, notification):
            sent.append(notification.destination)

    for url in ("https://hooks.test/a", "https://hooks.test/b"):
        notif_repo.add_rule(
            tenant_id=tenant_id, label="重大", channel=CHANNEL_SLACK, destination=url
        )
    dispatcher = NotificationDispatcher(repository=notif_repo, notifiers=[Fake()])
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert sorted(sent) == ["https://hooks.test/a", "https://hooks.test/b"]


def test_rules_do_not_leak_across_tenants(notif_repo, tenant_id, clean):
    other = uuid.uuid4()
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    assert notif_repo.rules_for(tenant_id=other, label="重大") == []


# ------------------------------------------------------- 失敗時のふるまい


def test_one_failure_does_not_stop_the_others(notif_repo, tenant_id, clean):
    delivered = []

    class Broken:
        channel = CHANNEL_SLACK

        def send(self, notification):
            raise RuntimeError("Webhook が落ちている")

    class Working:
        channel = CHANNEL_EMAIL

        def send(self, notification):
            delivered.append(notification.destination)

    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_SLACK, destination="https://x"
    )
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_EMAIL, destination="a@b.test"
    )
    dispatcher = NotificationDispatcher(
        repository=notif_repo, notifiers=[Broken(), Working()]
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    result = dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert delivered == ["a@b.test"], "1 つの失敗で他が止まっている"
    assert not result.ok
    assert result.failed[0][0] == CHANNEL_SLACK


def test_failed_delivery_does_not_repeat_forever(notif_repo, tenant_id, clean):
    """配信に失敗しても状態は進める。次のポーリングで再送し続けないため。"""
    attempts = []

    class Broken:
        channel = CHANNEL_SLACK

        def send(self, notification):
            attempts.append(1)
            raise RuntimeError("落ちている")

    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_SLACK, destination="https://x"
    )
    dispatcher = NotificationDispatcher(repository=notif_repo, notifiers=[Broken()])
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert len(attempts) == 1


def test_unknown_channel_is_reported_not_crashed(notif_repo, tenant_id, clean):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel="teams", destination="x"
    )
    dispatcher = NotificationDispatcher(repository=notif_repo, notifiers=[])
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    result = dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    assert result.failed == [("teams", "x", "未対応のチャネル")]


# ------------------------------------------------------------- 受信箱


def test_unread_can_be_marked_read(dispatcher, notif_repo, tenant_id):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))

    inbox = notif_repo.unread(tenant_id=tenant_id)
    assert notif_repo.mark_read(tenant_id=tenant_id, notification_ids=[inbox[0].id]) == 1
    assert notif_repo.unread(tenant_id=tenant_id) == []


def test_mark_read_is_idempotent(dispatcher, notif_repo, tenant_id):
    notif_repo.add_rule(
        tenant_id=tenant_id, label="重大", channel=CHANNEL_IN_APP, destination=""
    )
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge([]))
    dispatcher.dispatch(tenant_id=tenant_id, knowledge=knowledge(["重大"]))
    ids = [n.id for n in notif_repo.unread(tenant_id=tenant_id)]

    assert notif_repo.mark_read(tenant_id=tenant_id, notification_ids=ids) == 1
    assert notif_repo.mark_read(tenant_id=tenant_id, notification_ids=ids) == 0


def test_mark_read_with_no_ids_is_a_noop(notif_repo, tenant_id, clean):
    assert notif_repo.mark_read(tenant_id=tenant_id, notification_ids=[]) == 0


# --------------------------------------------------- Slack / メールの中身


def test_slack_message_includes_label_title_and_url():
    sent = {}

    def handler(request):
        import json

        sent["url"] = str(request.url)
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    notifier = SlackNotifier(client=httpx.Client(transport=httpx.MockTransport(handler)))
    from kb.notifications import Notification

    notifier.send(
        Notification(
            tenant_id=uuid.uuid4(),
            knowledge=knowledge(["重大"]),
            label="重大",
            destination="https://hooks.test/abc",
        )
    )

    assert sent["url"] == "https://hooks.test/abc"
    text = sent["body"]["text"]
    assert "重大" in text and "ORA-01555" in text
    assert "https://example.test/issues/1" in text


def test_slack_failure_is_raised():
    notifier = SlackNotifier(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
        )
    )
    from kb.notifications import Notification

    with pytest.raises(RuntimeError, match="Slack"):
        notifier.send(
            Notification(
                tenant_id=uuid.uuid4(),
                knowledge=knowledge(),
                label="重大",
                destination="https://hooks.test/x",
            )
        )


def test_email_has_subject_recipient_and_body():
    smtp = MagicMock(spec=smtplib.SMTP)
    smtp.__enter__ = MagicMock(return_value=smtp)
    smtp.__exit__ = MagicMock(return_value=False)

    notifier = EmailNotifier(sender="kb@example.test", smtp_factory=lambda: smtp)
    from kb.notifications import Notification

    notifier.send(
        Notification(
            tenant_id=uuid.uuid4(),
            knowledge=knowledge(["重大"]),
            label="重大",
            destination="dev@example.test",
        )
    )

    message = smtp.send_message.call_args[0][0]
    assert message["To"] == "dev@example.test"
    assert message["From"] == "kb@example.test"
    assert "重大" in message["Subject"] and "ORA-01555" in message["Subject"]
    assert "https://example.test/issues/1" in message.get_content()
