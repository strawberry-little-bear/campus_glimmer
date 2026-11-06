"""告警 Webhook 的数据库 outbox 与有界重试。

投递队列只持久化已经发生的状态翻转，不重新评估指标，也不修改告警状态。
``/metrics/`` 抓取时先入队并尝试立即投递；失败的行按指数退避等待
``flush_alert_notifications`` 管理命令重试。去重键由规则、事件和固定时间窗口
组成，多副本同时看到同一次翻转时只会有一行 outbox。
"""

import hashlib
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import AlertNotificationDelivery

logger = logging.getLogger(__name__)


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 30
DEFAULT_RETRY_MAX_SECONDS = 3600
DEFAULT_DEDUPE_SECONDS = 60
DEFAULT_LEASE_SECONDS = 120
TERMINAL_STATUSES = (
    AlertNotificationDelivery.DELIVERED,
    AlertNotificationDelivery.SKIPPED,
    AlertNotificationDelivery.EXHAUSTED,
)


def _positive_setting(name, default):
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(value, 0.1)


class AlertDeliveryQueue:
    """一个 PostgreSQL/数据库支持的告警 Webhook outbox。"""

    def __init__(self, *, max_attempts=None, retry_base_seconds=None,
                 retry_max_seconds=None, dedupe_seconds=None, lease_seconds=None):
        self.max_attempts = max(
            int(max_attempts if max_attempts is not None else
                getattr(settings, 'ALERT_NOTIFICATION_MAX_ATTEMPTS', DEFAULT_MAX_ATTEMPTS)),
            1,
        )
        self.retry_base_seconds = _positive_setting(
            'ALERT_NOTIFICATION_RETRY_BASE_SECONDS', DEFAULT_RETRY_BASE_SECONDS,
        ) if retry_base_seconds is None else max(float(retry_base_seconds), 0.1)
        self.retry_max_seconds = _positive_setting(
            'ALERT_NOTIFICATION_RETRY_MAX_SECONDS', DEFAULT_RETRY_MAX_SECONDS,
        ) if retry_max_seconds is None else max(float(retry_max_seconds), 0.1)
        self.retry_max_seconds = max(self.retry_max_seconds, self.retry_base_seconds)
        self.dedupe_seconds = _positive_setting(
            'ALERT_NOTIFICATION_DEDUPE_SECONDS', DEFAULT_DEDUPE_SECONDS,
        ) if dedupe_seconds is None else max(float(dedupe_seconds), 0.1)
        self.lease_seconds = _positive_setting(
            'ALERT_NOTIFICATION_LEASE_SECONDS', DEFAULT_LEASE_SECONDS,
        ) if lease_seconds is None else max(float(lease_seconds), 0.1)

    @classmethod
    def from_settings(cls):
        return cls()

    def dedupe_key(self, event):
        """同一规则、同一翻转、同一时间窗口在多副本上得到同一个键。

        窗口是必要的：各副本的进程内状态机没有共享时钟，同一次故障的
        ``occurred_at`` 会相差几秒。60 秒窗口把这种差异收拢为一次投递，代价是
        同一分钟内快速复发会与第一次合并；这是当前无外部队列方案的明确边界。
        """

        occurred_at = float(event.get('occurred_at') or 0.0)
        bucket = math.floor(occurred_at / self.dedupe_seconds)
        raw = '|'.join((
            str(event.get('name') or ''),
            str(event.get('event') or ''),
            str(event.get('state') or ''),
            str(bucket),
        ))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def enqueue(self, event, *, now=None):
        now = now or timezone.now()
        defaults = {
            'event': event['event'],
            'rule_name': event['name'],
            'state': event['state'],
            'payload': dict(event),
            # 先留出一个租约，避免入队事务刚提交就被另一个重试进程抢走。
            'next_attempt_at': now + timedelta(seconds=self.lease_seconds),
        }
        return AlertNotificationDelivery.objects.get_or_create(
            dedupe_key=self.dedupe_key(event),
            defaults=defaults,
        )

    def enqueue_and_deliver(self, events, notifier, *, now=None):
        """去重后立即尝试事件；到期未送达的行会在下一次抓取时继续重试。"""

        now = now or timezone.now()
        outcomes = {'delivered': 0, 'failed': 0, 'skipped': 0, 'deduplicated': 0}
        for event in events:
            row, created = self.enqueue(event, now=now)
            # 新行立刻投递；重复事件只在退避到期时重试。这样即使外部调度器
            # 暂时没跑，只要 Prometheus 还在抓取，失败通知仍能继续收敛。
            result = self.attempt(row.pk, notifier, now=now, force=created)
            if result is None:
                outcomes['deduplicated'] += 1
            elif result['status'] == AlertNotificationDelivery.DELIVERED:
                outcomes['delivered'] += 1
            elif result['status'] == AlertNotificationDelivery.SKIPPED:
                outcomes['skipped'] += 1
            else:
                outcomes['failed'] += 1
        return outcomes

    def attempt(self, delivery_id, notifier, *, now=None, force=False):
        """原子认领一行并投递；未到期或已被其它进程拿走时返回 ``None``。"""

        now = now or timezone.now()
        claimed = None
        with transaction.atomic():
            row = (
                AlertNotificationDelivery.objects
                .select_for_update()
                .filter(pk=delivery_id)
                .first()
            )
            if row is None or row.status in TERMINAL_STATUSES:
                return None
            if row.status == AlertNotificationDelivery.DELIVERING:
                if not force and row.next_attempt_at and row.next_attempt_at > now:
                    return None
            elif row.next_attempt_at and row.next_attempt_at > now and not force:
                return None
            row.status = AlertNotificationDelivery.DELIVERING
            row.attempts += 1
            row.last_attempt_at = now
            # 投递过程中进程崩溃时，租约到期后由重试命令接手。
            row.next_attempt_at = now + timedelta(seconds=self.lease_seconds)
            row.save(update_fields=[
                'status', 'attempts', 'last_attempt_at', 'next_attempt_at', 'updated_at',
            ])
            claimed = row

        try:
            outcome = notifier.deliver([dict(claimed.payload)], now.timestamp())
            if not isinstance(outcome, dict):
                raise TypeError('notifier result must be a dict')
        except Exception as error:
            outcome = {'failed': 1}
            logger.warning(
                '告警 Webhook 重试投递失败（第 %d 次）：%s',
                claimed.attempts, type(error).__name__,
            )

        delivered = int(outcome.get('delivered') or 0) > 0
        skipped = int(outcome.get('skipped') or 0) > 0
        failed = int(outcome.get('failed') or 0) > 0
        if delivered and not failed:
            claimed.status = AlertNotificationDelivery.DELIVERED
            claimed.delivered_at = now
            claimed.last_error = ''
        elif skipped and not failed:
            claimed.status = AlertNotificationDelivery.SKIPPED
            claimed.last_error = 'Webhook 未配置'
        else:
            claimed.last_error = '投递失败'
            if claimed.attempts >= self.max_attempts:
                claimed.status = AlertNotificationDelivery.EXHAUSTED
            else:
                claimed.status = AlertNotificationDelivery.PENDING
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** (claimed.attempts - 1)),
                )
                claimed.next_attempt_at = now + timedelta(seconds=delay)
        claimed.save(update_fields=[
            'status', 'next_attempt_at', 'delivered_at', 'last_error', 'updated_at',
        ])
        return {
            'status': claimed.status,
            'attempts': claimed.attempts,
            'outcome': outcome,
        }

    def flush(self, notifier, *, limit=50, now=None):
        """按到期时间处理一批，返回调用方可直接打印或记账的结果。"""

        now = now or timezone.now()
        due_ids = list(
            AlertNotificationDelivery.objects
            .filter(status__in=(
                AlertNotificationDelivery.PENDING,
                AlertNotificationDelivery.DELIVERING,
            ))
            .filter(next_attempt_at__lte=now)
            .order_by('next_attempt_at', 'id')
            .values_list('id', flat=True)[:max(int(limit), 1)]
        )
        result = {
            'attempted': 0,
            'delivered': 0,
            'failed': 0,
            'skipped': 0,
            'exhausted': 0,
        }
        for delivery_id in due_ids:
            outcome = self.attempt(delivery_id, notifier, now=now)
            if outcome is None:
                continue
            result['attempted'] += 1
            status = outcome['status']
            if status == AlertNotificationDelivery.DELIVERED:
                result['delivered'] += 1
            elif status == AlertNotificationDelivery.SKIPPED:
                result['skipped'] += 1
            elif status == AlertNotificationDelivery.EXHAUSTED:
                result['exhausted'] += 1
                result['failed'] += 1
            else:
                result['failed'] += 1
        return result

    def prune_terminal(self, *, before):
        """清理早于 ``before`` 的终态台账，避免 outbox 无限增长。"""

        deleted, _details = AlertNotificationDelivery.objects.filter(
            status__in=TERMINAL_STATUSES,
            updated_at__lt=before,
        ).delete()
        return deleted

    def snapshot(self, *, now=None):
        """给 /alerts/ 和运维命令使用的队列摘要；状态名与模型一致。"""

        now = now or timezone.now()
        counts = {status: 0 for status, _label in AlertNotificationDelivery.STATUS_CHOICES}
        for row in (
            AlertNotificationDelivery.objects
            .values('status')
            .annotate(total=Count('id'))
        ):
            counts[row['status']] = row['total']
        return {
            'enabled': True,
            'degraded': False,
            'status': counts,
            'due': AlertNotificationDelivery.objects.filter(
                status__in=(
                    AlertNotificationDelivery.PENDING,
                    AlertNotificationDelivery.DELIVERING,
                ),
                next_attempt_at__lte=now,
            ).count(),
        }
