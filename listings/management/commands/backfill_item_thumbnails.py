# -*- coding: utf-8 -*-
"""Incrementally build list-page thumbnails and display metadata."""

from django.core.management.base import BaseCommand
from django.db.models import Q

from listings.image_processing import PREVIEW_MAX_EDGE_PIXELS
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
    help = '为历史商品图片分批生成列表页缩略图、大图预览并补齐展示宽高；可重复执行。'

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
                total_count = pending.count()
                pending_count = pending[:limit].count() if limit is not None else total_count
                run.metrics = normalise_metrics({'marked': pending_count})
                message = f'本次待补齐商品图片缩略图、预览图或展示宽高 {pending_count} 条。'
                if limit is not None:
                    message += f'当前总待处理量为 {total_count} 条。'
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
                update_fields = set()
                failures = []

                if not item_image.thumbnail:
                    try:
                        item_image._replace_thumbnail()
                        update_fields.update({
                            'thumbnail', 'image_width', 'image_height',
                            'thumbnail_width', 'thumbnail_height',
                        })
                    except Exception as exc:
                        failures.append(f'缩略图 {type(exc).__name__}')
                else:
                    try:
                        update_fields.update(item_image.refresh_display_dimensions())
                    except Exception as exc:
                        failures.append(f'展示宽高 {type(exc).__name__}')

                if item_image.image_width is not None and item_image.image_height is not None:
                    source_edge = max(item_image.image_width, item_image.image_height)
                    if source_edge > PREVIEW_MAX_EDGE_PIXELS and not item_image.preview:
                        try:
                            item_image._replace_preview()
                            update_fields.update({
                                'preview', 'preview_width', 'preview_height',
                            })
                        except Exception as exc:
                            failures.append(f'预览图 {type(exc).__name__}')
                    elif item_image.preview and (
                        item_image.preview_width is None
                        or item_image.preview_height is None
                    ):
                        try:
                            update_fields.update(item_image.refresh_display_dimensions())
                        except Exception as exc:
                            failures.append(f'预览图宽高 {type(exc).__name__}')

                if failures:
                    failed += 1
                    self.stdout.write(self.style.WARNING(
                        f'商品图片档案补齐失败：{", ".join(failures)}。'
                    ))
                if update_fields:
                    item_image.save(update_fields=list(update_fields))
                    updated += 1
                elif not failures:
                    skipped += 1
                    continue

            remaining = self.pending_queryset().count()
            run.metrics = normalise_metrics({
                'marked': marked, 'updated': updated,
                'failed': failed, 'skipped': skipped,
            })
            self.stdout.write(self.style.SUCCESS(
                f'检查 {marked} 条商品图片，补齐 {updated} 条缩略图、预览图或展示宽高，'
                f'失败 {failed} 条，跳过 {skipped} 条，剩余 {remaining} 条待处理。'
            ))

    @staticmethod
    def pending_queryset():
        missing_thumbnail = Q(thumbnail__isnull=True) | Q(thumbnail='')
        has_preview = ~Q(preview__isnull=True) & ~Q(preview='')
        missing_dimensions = (
            Q(image_width__isnull=True) | Q(image_height__isnull=True)
            | Q(thumbnail_width__isnull=True) | Q(thumbnail_height__isnull=True)
            | (has_preview & (Q(preview_width__isnull=True) | Q(preview_height__isnull=True)))
        )
        missing_preview = Q(preview__isnull=True) | Q(preview='')
        large_source = Q(image_width__gt=PREVIEW_MAX_EDGE_PIXELS) | Q(image_height__gt=PREVIEW_MAX_EDGE_PIXELS)
        return ItemImage.objects.filter(
            missing_thumbnail | missing_dimensions | (large_source & missing_preview),
        ).order_by('id')
