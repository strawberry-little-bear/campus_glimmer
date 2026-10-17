from django.core.management.base import BaseCommand

from listings.campaign_updates import send_campaign_update_digest
from listings.task_runs import normalise_metrics, record_task_run


class Command(BaseCommand):
    help = '发送用户主动订阅的校园专题上新摘要'

    def add_arguments(self, parser):
        parser.add_argument(
            '--frequency', choices=['daily', 'weekly'], default=None,
            help='只发送指定频率的摘要，默认每日与每周都处理。',
        )

    def handle(self, *args, **options):
        with record_task_run('send_campaign_update_digest') as run:
            result = send_campaign_update_digest(frequency=options.get('frequency'))
            run.metrics = normalise_metrics(result)
        self.stdout.write(self.style.SUCCESS(
            f"已发送 {result['sent']} 条专题上新摘要，"
            f"覆盖 {result['delivered_matches']} 件商品，"
            f"跳过 {result['skipped']} 位用户，"
            f"{result['quiet']} 位用户处于免打扰时段，"
            f"{result['empty']} 位用户暂无待发送更新，"
            f"取消 {result['invalid']} 条失效命中。"
        ))
