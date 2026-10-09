from django.core.management.base import BaseCommand

from listings.opportunity_digest import send_opportunity_digest
from listings.task_runs import normalise_metrics, record_task_run


class Command(BaseCommand):
    help = '向有匹配结果的用户发送校园互助机会摘要'

    def handle(self, *args, **options):
        with record_task_run('send_opportunity_digest') as run:
            result = send_opportunity_digest()
            run.metrics = normalise_metrics(result)
        self.stdout.write(self.style.SUCCESS(
            f"已发送 {result['sent']} 位用户的互助机会摘要，"
            f"跳过 {result['skipped']} 位，"
            f"{result['empty']} 位用户当前没有可推荐机会。"
        ))
