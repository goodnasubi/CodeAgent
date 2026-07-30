"""常駐スケジューラ。全テナントを一定間隔でポーリングする。

`SyncRunner` は「1 テナントを 1 回取り込む」までしか受け持たない。定期的に
呼び続ける役目をここに置く。cron ではなく常駐プロセスにしているのは、
テナントの一覧が DB にあり、追加のたびに crontab を書き換えたくないため。

**API プロセスの中では動かさない。** uvicorn を複数ワーカーで起動すると
ワーカーの数だけスケジューラが立ち、同じテナントを同時に取り込むことになる。
別プロセスとして起動する:

    KB_DSN=... KB_SECRET_KEY=... uv run python -m kb.scheduler

それでも二重起動は起こりうるので、テナント単位の advisory lock で守っている
（`kb.db.sync.tenant_sync_lock`）。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, ContextManager, Iterator, Sequence
from uuid import UUID

import psycopg

from .db.notifications import NotificationRepository
from .db.sync import SyncInProgress, tenant_sync_lock
from .factory import (
    BackendNotConfigured,
    build_notifiers,
    build_shared_notifiers,
    build_sync_runner,
)
from .notifications import Notifier
from .sync import SyncReport, interval_from_env
from .tenants import TenantRepository, load_cipher

# `python -m kb.scheduler` で起動すると __name__ が "__main__" になり、ログの
# 出どころが分からなくなるため、名前を直接書く
logger = logging.getLogger("kb.scheduler")

DSN_ENV = "KB_DSN"


@dataclass
class PassReport:
    """1 周ぶんの結果。"""

    started_at: datetime
    reports: dict[UUID, SyncReport] = field(default_factory=dict)
    skipped: dict[UUID, str] = field(default_factory=dict)
    """取り込まなかったテナントと、その理由。"""

    @property
    def ok(self) -> bool:
        """取り込みを試した分がすべて成功したか。

        `skipped` は見ない。KB 未設定や実行中による見送りは正常な状態で、
        異常と一緒に扱うと「本当に失敗した回」が埋もれる。
        """
        return all(r.ok for r in self.reports.values())

    def summary(self) -> str:
        ingested = sum(len(r.ingested) for r in self.reports.values())
        notified = sum(len(r.notified) for r in self.reports.values())
        return (
            f"テナント {len(self.reports)} 件 / 取り込み {ingested} 件 /"
            f" 通知 {notified} 件 / 見送り {len(self.skipped)} 件"
        )


ConnectFn = Callable[[], ContextManager[psycopg.Connection]]


def _default_connect() -> ContextManager[psycopg.Connection]:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV} が未設定です")
    return psycopg.connect(dsn, autocommit=True)


class SyncScheduler:
    """全テナントのポーリングを回し続ける。

    止まらないことが仕事なので、失敗はすべて飲み込んで次の周回に回す:

    - 1 テナントの失敗は、そのテナントの `skipped` になるだけで周回は続く
    - 周回そのものの失敗（DB に繋がらない等）でもループは終わらない

    取りこぼしは `SyncRunner` 側の到達点が進まないことで次回やり直される。
    """

    def __init__(
        self,
        *,
        connect: ConnectFn = _default_connect,
        interval: float | None = None,
        shared_notifiers: Sequence[Notifier] | None = None,
    ) -> None:
        self._connect = connect
        self._interval = interval if interval is not None else interval_from_env()
        # Slack などは接続を抱えるので、周回ごとに作り直さず使い回す
        self._shared_notifiers = (
            list(shared_notifiers)
            if shared_notifiers is not None
            else build_shared_notifiers()
        )
        self._stop = threading.Event()

    # ------------------------------------------------------------- 1 周ぶん

    def run_pass(self) -> PassReport:
        """全テナントを 1 回ずつ取り込む。"""
        report = PassReport(started_at=datetime.now(timezone.utc))
        with self._connect() as conn:
            tenants = TenantRepository(conn, cipher=load_cipher())
            for tenant in tenants.list_tenants():
                self._sync_tenant(
                    conn=conn, tenants=tenants, tenant_id=tenant.id, report=report
                )
        return report

    def _sync_tenant(
        self,
        *,
        conn: psycopg.Connection,
        tenants: TenantRepository,
        tenant_id: UUID,
        report: PassReport,
    ) -> None:
        try:
            with tenant_sync_lock(conn, tenant_id=tenant_id):
                runner = build_sync_runner(
                    conn=conn,
                    tenant_id=tenant_id,
                    tenants=tenants,
                    notifiers=build_notifiers(
                        NotificationRepository(conn), shared=self._shared_notifiers
                    ),
                )
                report.reports[tenant_id] = runner.sync(tenant_id=tenant_id)
        except SyncInProgress:
            # 手動実行中か、スケジューラが二重に動いている。次の周回で拾う
            report.skipped[tenant_id] = "実行中"
        except BackendNotConfigured as exc:
            # 払い出しただけで KB 未設定のテナント。異常ではない
            report.skipped[tenant_id] = str(exc)
        except Exception as exc:  # 1 テナントの失敗で周回を止めない
            logger.exception("テナントの取り込みに失敗 tenant=%s", tenant_id)
            report.skipped[tenant_id] = str(exc)

    # ---------------------------------------------------------------- 常駐

    def run_forever(self, *, max_passes: int | None = None) -> None:
        """止められるまで回し続ける。`max_passes` はテスト用の回数上限。"""
        logger.info("スケジューラを開始します（間隔 %.0f 秒）", self._interval)
        passes = 0
        while not self._stop.is_set():
            started = datetime.now(timezone.utc)
            try:
                report = self.run_pass()
            except Exception:
                # DB に繋がらない等。ここで落とすと常駐の意味がない
                logger.exception("周回に失敗しました。次の間隔で再試行します")
            else:
                logger.info("同期完了: %s", report.summary())

            passes += 1
            if max_passes is not None and passes >= max_passes:
                break

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > self._interval:
                logger.warning(
                    "1 周に %.0f 秒かかりました（間隔 %.0f 秒）。間隔を延ばすか"
                    "テナントを分けてください",
                    elapsed,
                    self._interval,
                )
            # 間隔ぶん待つ。stop が立てば待たずに抜ける
            self._stop.wait(max(0.0, self._interval - elapsed))

    def stop(self) -> None:
        """停止要求。走っている取り込みは中断しない。

        同じシグナルがプロセスグループ経由で二重に届くことがあるので、
        すでに停止済みなら黙って返る。
        """
        if self._stop.is_set():
            return
        logger.info("停止要求を受け取りました")
        self._stop.set()


@contextmanager
def _handle_signals(scheduler: SyncScheduler) -> Iterator[None]:
    """SIGTERM / SIGINT で待機を打ち切って終わる。

    走っている取り込みは最後まで終わらせる。途中で切ると到達点が進まず、
    次回同じ範囲をやり直すことになるため（壊れはしないが無駄）。
    """
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: scheduler.stop())
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("KB_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    scheduler = SyncScheduler()
    with _handle_signals(scheduler):
        scheduler.run_forever()
    logger.info("スケジューラを停止しました")


if __name__ == "__main__":
    main()
