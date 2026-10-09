from django.core.management.base import BaseCommand

from listings.order_maintenance import DEFAULT_REMINDER_HOURS, process_borrow_due_notifications, process_order_timeouts
from listings.task_runs import normalise_metrics, record_task_run


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
        with record_task_run('process_order_timeouts') as run:
            result = process_order_timeouts(reminder_hours=options['reminder_hours'])
            borrow_result = process_borrow_due_notifications(reminder_hours=options['reminder_hours'])
            run.metrics = normalise_metrics({
                'reminded': result['reminded'] + borrow_result['reminded'],
                'expired': result['expired'] + borrow_result['overdue'],
            })
        self.stdout.write(
            self.style.SUCCESS(
                f"已发送 {result['reminded']} 条预约超时提醒、{borrow_result['reminded']} 条借用到期提醒，自动释放 {result['expired']} 笔预约，记录 {borrow_result['overdue']} 笔借用逾期。"
            )
        )
