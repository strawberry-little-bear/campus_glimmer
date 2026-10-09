from django.core.management.base import BaseCommand

from listings.item_lifecycle import expire_items
from listings.task_runs import normalise_metrics, record_task_run


class Command(BaseCommand):
    help = '将超过展示截止时间的在售商品自动下架，并通知发布者'

    def handle(self, *args, **options):
        with record_task_run('expire_items') as run:
            result = expire_items()
            run.metrics = normalise_metrics(result)
        self.stdout.write(self.style.SUCCESS(f"已自动下架 {result['expired']} 件到期商品。"))
