# -*- coding: utf-8 -*-
"""Incrementally build list-page thumbnails for historical item photos."""

from django.core.management.base import BaseCommand
from django.db.models import Q

from listings.models import ItemImage
from listings.task_runs import normalise_metrics, record_task_run


def positive_int(value):
    try:
        value = int(value)
    except ValueError as exc:
        raise ValueError('must be a positive integer') from exc
    if value <= 0:
        raise ValueError('must be a positive integer')
    return value


class Command(BaseCommand):
    help = '为历史商品图片分批生成列表页缩略图；可重复执行，已有缩略图的记录会跳过。'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=positive_int, default=None,
            help='本次最多处理的记录数，适合把回填拆成多批。',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只统计待处理数量，不写文件、不修改数据库。',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        limit = options['limit']
        with record_task_run('backfill_item_thumbnails', dry_run=dry_run) as run:
            pending = self.pending_queryset()

            if dry_run:
                pending_count = pending.count()
                run.metrics = normalise_metrics({'marked': pending_count})
                message = f'待回填商品图片缩略图 {pending_count} 条。'
                if limit is not None:
                    message += f'本次限制为 {limit} 条。'
                self.stdout.write(self.style.WARNING(message))
                return

            marked = failed = skipped = 0
            for item_image in pending.iterator():
                if limit is not None and marked >= limit:
                    break
                marked += 1
                if not item_image.image:
                    skipped += 1
                    continue
                try:
                    item_image._replace_thumbnail()
                    item_image.save(update_fields=['thumbnail'])
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.WARNING(
                        f'缩略图生成失败：{type(exc).__name__}。'
                    ))

            remaining = self.pending_queryset().count()
            run.metrics = normalise_metrics({
                'marked': marked, 'failed': failed, 'skipped': skipped,
            })
            self.stdout.write(self.style.SUCCESS(
                f'检查 {marked} 条商品图片，生成 {marked - failed - skipped} 条缩略图，'
                f'失败 {failed} 条，跳过 {skipped} 条，剩余 {remaining} 条待处理。'
            ))

    @staticmethod
    def pending_queryset():
        return ItemImage.objects.filter(
            Q(thumbnail__isnull=True) | Q(thumbnail=''),
        ).order_by('id')
