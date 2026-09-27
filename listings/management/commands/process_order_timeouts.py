from django.core.management.base import BaseCommand

from listings.order_maintenance import DEFAULT_REMINDER_HOURS, process_order_timeouts


class Command(BaseCommand):
    help = '发送交易预约超时提醒，并自动释放已过期的预约'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reminder-hours',
            type=int,
            default=DEFAULT_REMINDER_HOURS,
            help='距离卖家确认截止还有多少小时开始提醒，默认 6 小时',
        )

    def handle(self, *args, **options):
        result = process_order_timeouts(reminder_hours=options['reminder_hours'])
        self.stdout.write(
            self.style.SUCCESS(
                f"已发送 {result['reminded']} 条超时提醒，自动释放 {result['expired']} 笔交易预约。"
            )
        )
