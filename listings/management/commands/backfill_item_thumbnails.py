# -*- coding: utf-8 -*-
"""Incrementally build list-page thumbnails and display metadata."""

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
    help = '为历史商品图片分批生成列表页缩略图并补齐展示宽高；可重复执行。'

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
                message = f'待补齐商品图片缩略图或展示宽高 {pending_count} 条。'
                if limit is not None:
                    message += f'本次限制为 {limit} 条。'
                self.stdout.write(self.style.WARNING(message))
                return

            marked = updated = failed = skipped = 0
            for item_image in pending.iterator():
                if limit is not None and marked >= limit:
                    break
                marked += 1
                if not item_image.image:
                    skipped += 1
                    continue
                try:
                    if not item_image.thumbnail:
                        item_image._replace_thumbnail()
                        update_fields = [
                            'thumbnail', 'image_width', 'image_height',
                            'thumbnail_width', 'thumbnail_height',
                        ]
                    else:
                        update_fields = item_image.refresh_display_dimensions()
                    if not update_fields:
                        skipped += 1
                        continue
                    item_image.save(update_fields=update_fields)
                    updated += 1
                except Exception as exc:
                    failed += 1
                    self.stdout.write(self.style.WARNING(
                        f'商品图片档案补齐失败：{type(exc).__name__}。'
                    ))

            remaining = self.pending_queryset().count()
            run.metrics = normalise_metrics({
                'marked': marked, 'updated': updated,
                'failed': failed, 'skipped': skipped,
            })
            self.stdout.write(self.style.SUCCESS(
                f'检查 {marked} 条商品图片，补齐 {updated} 条缩略图或展示宽高，'
                f'失败 {failed} 条，跳过 {skipped} 条，剩余 {remaining} 条待处理。'
            ))

    @staticmethod
    def pending_queryset():
        missing_thumbnail = Q(thumbnail__isnull=True) | Q(thumbnail='')
        missing_dimensions = (
            Q(image_width__isnull=True) | Q(image_height__isnull=True)
            | Q(thumbnail_width__isnull=True) | Q(thumbnail_height__isnull=True)
        )
        return ItemImage.objects.filter(
            missing_thumbnail | missing_dimensions,
        ).order_by('id')
