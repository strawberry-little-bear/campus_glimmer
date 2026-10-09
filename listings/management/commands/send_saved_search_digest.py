from django.core.management.base import BaseCommand

from listings.saved_search_digest import send_saved_search_digest


class Command(BaseCommand):
    help = '发送关注搜索的每日或每周汇总提醒'

    def add_arguments(self, parser):
        parser.add_argument(
            '--frequency', choices=['daily', 'weekly'], default=None,
            help='只发送指定频率的汇总，默认每日与每周都处理。',
        )

    def handle(self, *args, **options):
        result = send_saved_search_digest(frequency=options.get('frequency'))
        self.stdout.write(self.style.SUCCESS(
            f"已发送 {result['sent']} 条关注搜索汇总提醒，"
            f"跳过 {result['skipped']} 条，"
            f"{result['empty']} 个关注暂无命中，"
            f"{result['quiet']} 个关注处于静默期。"
        ))
