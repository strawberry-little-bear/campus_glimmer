from django.core.management.base import BaseCommand

from listings.meeting_response_reminders import (
    DEFAULT_RESPONSE_HOURS,
    send_meeting_response_reminders,
)
from listings.task_runs import normalise_metrics, record_task_run


class Command(BaseCommand):
    help = '提醒长时间未回应的交付安排'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=DEFAULT_RESPONSE_HOURS,
            help='提议等待多少小时后提醒，默认 24 小时',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只统计会发送的提醒，不写入通知。',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        response_hours = options['hours']
        with record_task_run(
            'send_meeting_response_reminders', dry_run=dry_run,
        ) as run:
            result = send_meeting_response_reminders(
                response_hours=response_hours,
                dry_run=dry_run,
            )
            run.metrics = normalise_metrics(result)
        prefix = '预演' if dry_run else '执行'
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}完成：发送 {result['sent']} 条交付安排待回应提醒，"
            f"此前已提醒 {result['reminded']} 笔，跳过 {result['skipped']} 笔，"
            f"失败 {result['failed']} 笔；共检查 {result['candidates']} 笔待回应安排。"
        ))
