from django.core.management.base import BaseCommand

from listings.lifecycle_reminders import send_lifecycle_reminders


class Command(BaseCommand):
    help = '发送商品生命周期提醒（待处理商品与高于同类区间的价格）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--seller-id', action='append', dest='seller_ids', default=None, type=int,
            help='只提醒指定用户，可重复传入；默认处理全部卖家。',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只统计会收到提醒的卖家，不写入通知，也不回写冷却字段。',
        )

    def handle(self, *args, **options):
        result = send_lifecycle_reminders(
            seller_ids=options.get('seller_ids'),
            dry_run=options.get('dry_run', False),
        )
        prefix = '预演' if options.get('dry_run') else '执行'
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}完成：发送 {result['sent']} 条提醒，"
            f"其中待处理 {result['attention_sellers']} 位卖家、"
            f"价格参考 {result['price_sellers']} 位卖家，"
            f"回写冷却 {result['marked']} 件商品，"
            f"{result['skipped_preference']} 位卖家关闭了该类提醒，"
            f"{result['quiet']} 位卖家处于免打扰时段，"
            f"{result['skipped_no_evidence']} 位卖家缺少可引用的价格证据。"
        ))
