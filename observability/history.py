from django.db.models import Q

from .models import AlertTransition


DB_HISTORY_LIMIT = 1000


class DatabaseAlertHistory:
    """有界、尽力而为的数据库历史存储。

    告警评估不依赖这里成功：写入失败时调用方保留进程内窗口，并单独计数。
    """

    retention_limit = DB_HISTORY_LIMIT

    def record(self, event):
        row = AlertTransition.objects.create(
            event=event['event'],
            rule_name=event['name'],
            severity=event['severity'],
            summary=event['summary'],
            state=event['state'],
            value=event.get('value'),
            detail=event.get('detail') or '',
            since=event.get('since'),
            occurred_at=event['occurred_at'],
            active_seconds=event.get('active_seconds') or 0.0,
            instance=event.get('instance') or '',
        )
        self._prune()
        return row.pk

    def recent(self, limit):
        rows = AlertTransition.objects.order_by('-occurred_at', '-id')[:limit]
        return [row.as_event() for row in rows]

    def _prune(self):
        """只保留最近 DB_HISTORY_LIMIT 条，避免排障表无限增长。"""

        boundary = (
            AlertTransition.objects
            .order_by('-occurred_at', '-id')
            .values('id', 'occurred_at')[DB_HISTORY_LIMIT - 1:DB_HISTORY_LIMIT]
            .first()
        )
        if boundary is None:
            return
        AlertTransition.objects.filter(
            Q(occurred_at__lt=boundary['occurred_at'])
            | Q(occurred_at=boundary['occurred_at'], id__lt=boundary['id'])
        ).delete()