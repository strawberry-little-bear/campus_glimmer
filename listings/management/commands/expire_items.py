from django.core.management.base import BaseCommand

from listings.item_lifecycle import expire_items


class Command(BaseCommand):
    help = '将超过展示截止时间的在售商品自动下架，并通知发布者'

    def handle(self, *args, **options):
        result = expire_items()
        self.stdout.write(self.style.SUCCESS(f"已自动下架 {result['expired']} 件到期商品。"))
