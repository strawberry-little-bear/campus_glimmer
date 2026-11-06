"""重试到期未送达的告警 Webhook，并清理过旧的终态台账。"""

import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from campus_glimmer.alerts import AlertNotifier
from observability.delivery import AlertDeliveryQueue


class Command(BaseCommand):
    help = '重试到期的告警 Webhook 投递，并清理过旧的终态记录。'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50, help='本次最多尝试的条数')
        parser.add_argument(
            '--prune-days', type=int, default=30,
            help='删除多少天前的已投递/已跳过/耗尽记录，最小为 1',
        )

    def handle(self, *args, **options):
        if not (getattr(settings, 'ALERT_WEBHOOK_URL', '') or '').strip():
            raise CommandError('DJANGO_ALERT_WEBHOOK_URL 未配置，无法投递告警')
        now = timezone.now()
        queue = AlertDeliveryQueue.from_settings()
        result = queue.flush(
            AlertNotifier.from_settings(),
            limit=max(int(options['limit']), 1),
            now=now,
        )
        prune_days = max(int(options['prune_days']), 1)
        result['pruned'] = queue.prune_terminal(
            before=now - timedelta(days=prune_days),
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
