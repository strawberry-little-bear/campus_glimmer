from django.core.management.base import BaseCommand

from listings.operations_digest import DEFAULT_DIGEST_DAYS, send_operations_digest
from listings.task_runs import normalise_metrics, record_task_run


class Command(BaseCommand):
    help = '向管理员发送运营看板的每日告警摘要'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=DEFAULT_DIGEST_DAYS,
            choices=(7, 30, 90, 365),
            help='运营摘要统计周期，默认 7 天',
        )

    def handle(self, *args, **options):
        with record_task_run('send_operations_digest') as run:
            result = send_operations_digest(days=options['days'])
            run.metrics = normalise_metrics(result)
        if result['reason'] == 'no_alerts':
            self.stdout.write(self.style.SUCCESS('当前统计周期没有需要发送的运营告警。'))
            return
        self.stdout.write(self.style.SUCCESS(
            f"已发送 {result['sent']} 位管理员的运营告警日报，"
            f"跳过 {result['skipped']} 位，包含 {result['alerts']} 项告警。"
        ))
