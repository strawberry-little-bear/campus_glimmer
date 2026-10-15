# -*- coding: utf-8 -*-
"""Pin down responsive item image variants and their historical backfill."""

from io import BytesIO, StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from .image_processing import (
    PREVIEW_MAX_EDGE_PIXELS, THUMBNAIL_MAX_EDGE_PIXELS,
    build_item_preview, build_item_thumbnail,
)
from .models import CampusLocation, Category, Item, ItemImage, TaskRun


def image_upload(name='photo.png', width=960, height=600, format='PNG', mode='RGB', orientation=None):
    image = Image.new(mode, (width, height), '#58a')
    if mode == 'RGBA':
        image.putalpha(128)
    buffer = BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs['exif'] = exif
    image.save(buffer, format=format, **kwargs)
    return SimpleUploadedFile(
        name, buffer.getvalue(), content_type=f'image/{format.lower()}',
    )


class BuildItemThumbnailTests(TestCase):
    def test_output_is_bounded_webp_without_upscaling(self):
        result = build_item_thumbnail(image_upload(width=180, height=120))
        self.assertEqual(
            (result.source_width, result.source_height, result.thumbnail_width, result.thumbnail_height),
            (180, 120, 180, 120),
        )
        with Image.open(result) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertEqual(thumbnail.size, (180, 120))

    def test_preview_is_bounded_webp_without_upscaling(self):
        result = build_item_preview(image_upload(width=1600, height=1000))
        self.assertEqual(
            (
                result.source_width, result.source_height,
                result.preview_width, result.preview_height,
            ),
            (1600, 1000, 960, 600),
        )
        with Image.open(result) as preview:
            self.assertEqual(preview.format, 'WEBP')
            self.assertEqual(preview.size, (960, 600))

        result = build_item_preview(image_upload(width=640, height=420))
        self.assertEqual(
            (
                result.source_width, result.source_height,
                result.preview_width, result.preview_height,
            ),
            (640, 420, 640, 420),
        )
        with Image.open(result) as preview:
            self.assertEqual(preview.size, (640, 420))
            self.assertLessEqual(max(preview.size), PREVIEW_MAX_EDGE_PIXELS)

    def test_exif_orientation_and_alpha_are_preserved(self):
        result = build_item_thumbnail(
            image_upload(width=800, height=400, format='JPEG', orientation=6),
        )
        self.assertEqual(
            (result.source_width, result.source_height, result.thumbnail_width, result.thumbnail_height),
            (400, 800, 240, 480),
        )
        with Image.open(result) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertGreater(thumbnail.size[1], thumbnail.size[0])
            self.assertLessEqual(max(thumbnail.size), THUMBNAIL_MAX_EDGE_PIXELS)

        result = build_item_thumbnail(image_upload(width=700, height=300, mode='RGBA'))
        with Image.open(result) as thumbnail:
            self.assertIn('A', thumbnail.getbands())


class ItemThumbnailModelTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='thumbnail-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='缩略图分类')
        self.location = CampusLocation.objects.create(name='缩略图地点')
        self.item = Item.objects.create(
            title='缩略图商品', description='用于缩略图测试', price='30.00',
            category=self.category, condition='九成新',
            location=self.location, seller=self.seller,
        )

    def test_new_image_gets_a_bounded_webp_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item, image=image_upload(name='new-cover.png'),
        )
        self.assertTrue(item_image.thumbnail.name.startswith('items/thumbnails/'))
        self.assertEqual(item_image.image_width, 960)
        self.assertEqual(item_image.image_height, 600)
        self.assertEqual(item_image.thumbnail_width, 480)
        self.assertEqual(item_image.thumbnail_height, 300)
        with Image.open(item_image.thumbnail) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertLessEqual(max(thumbnail.size), THUMBNAIL_MAX_EDGE_PIXELS)
        self.assertFalse(item_image.preview)
        self.assertIsNone(item_image.preview_width)
        self.assertIsNone(item_image.preview_height)
        self.assertEqual(item_image.list_image_url, item_image.thumbnail.url)
        self.assertEqual(item_image.list_image_srcset, '')
        self.assertEqual(item_image.detail_image_url, item_image.image.url)
        self.assertEqual(item_image.detail_image_srcset, '')

    def test_new_large_image_gets_both_variants_and_srcset_candidates(self):
        item_image = ItemImage.objects.create(
            item=self.item,
            image=image_upload(name='new-large-cover.png', width=1600, height=1000),
        )

        self.assertTrue(item_image.thumbnail.name.startswith('items/thumbnails/'))
        self.assertTrue(item_image.preview.name.startswith('items/previews/'))
        self.assertEqual(
            (
                item_image.image_width, item_image.image_height,
                item_image.thumbnail_width, item_image.thumbnail_height,
                item_image.preview_width, item_image.preview_height,
            ),
            (1600, 1000, 480, 300, 960, 600),
        )
        self.assertIn(f'{item_image.thumbnail.url} 480w', item_image.list_image_srcset)
        self.assertIn(f'{item_image.preview.url} 960w', item_image.list_image_srcset)
        self.assertEqual(item_image.detail_image_url, item_image.preview.url)
        self.assertIn(
            f'{item_image.preview.url} 960w, {item_image.image.url} 1600w',
            item_image.detail_image_srcset,
        )
        with Image.open(item_image.preview) as preview:
            self.assertEqual(preview.format, 'WEBP')
            self.assertEqual(preview.size, (960, 600))

    def test_replacing_the_original_rebuilds_and_removes_the_old_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item,
            image=image_upload(name='first-cover.png', width=1600, height=1000),
        )
        old_thumbnail = item_image.thumbnail.name
        old_preview = item_image.preview.name
        item_image.image = image_upload(name='second-cover.png', width=700, height=900)
        item_image.save()
        item_image.refresh_from_db()

        self.assertNotEqual(item_image.thumbnail.name, old_thumbnail)
        self.assertFalse(item_image.thumbnail.storage.exists(old_thumbnail))
        self.assertEqual(
            (
                item_image.image_width, item_image.image_height,
                item_image.thumbnail_width, item_image.thumbnail_height,
            ),
            (700, 900, 373, 480),
        )
        with Image.open(item_image.thumbnail) as thumbnail:
            self.assertEqual(thumbnail.size, (373, 480))
        self.assertFalse(item_image.preview)
        self.assertFalse(item_image.preview.storage.exists(old_preview))
        self.assertIsNone(item_image.preview_width)
        self.assertIsNone(item_image.preview_height)
        self.assertEqual(item_image.detail_image_url, item_image.image.url)
        self.assertEqual(item_image.detail_image_srcset, '')

    def test_partial_update_persists_the_rebuilt_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item, image=image_upload(name='partial-first.png'),
        )
        old_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(name='partial-second.png', width=1600, height=1000)
        item_image.save(update_fields=['image'])
        item_image.refresh_from_db()

        self.assertTrue(item_image.thumbnail)
        self.assertNotEqual(item_image.thumbnail.name, old_thumbnail)
        self.assertTrue(item_image.preview)

    def test_failed_generation_keeps_the_original_readable_and_clears_stale_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item,
            image=image_upload(name='stale-cover.png', width=1600, height=1000),
        )
        stale_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(
            name='fallback-cover.png', width=1600, height=1000,
        )
        with patch('listings.models.build_item_thumbnail', side_effect=RuntimeError('boom')):
            item_image.save()
        item_image.refresh_from_db()

        self.assertFalse(item_image.thumbnail)
        self.assertFalse(item_image.thumbnail.storage.exists(stale_thumbnail))
        self.assertEqual((item_image.image_width, item_image.image_height), (1600, 1000))
        self.assertIsNone(item_image.thumbnail_width)
        self.assertIsNone(item_image.thumbnail_height)
        self.assertTrue(item_image.preview)
        self.assertEqual((item_image.preview_width, item_image.preview_height), (960, 600))
        self.assertEqual(item_image.list_image_url, item_image.image.url)
        self.assertEqual(item_image.list_image_srcset, '')
        self.assertEqual(item_image.detail_image_url, item_image.preview.url)

    def test_failed_preview_generation_keeps_the_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item,
            image=image_upload(name='preview-fallback.png', width=1600, height=1000),
        )
        old_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(
            name='preview-fallback-second.png', width=1600, height=1000,
        )
        with patch('listings.models.build_item_preview', side_effect=RuntimeError('boom')):
            item_image.save()
        item_image.refresh_from_db()

        self.assertTrue(item_image.thumbnail)
        self.assertNotEqual(item_image.thumbnail.name, old_thumbnail)
        self.assertFalse(item_image.preview)
        self.assertIsNone(item_image.preview_width)
        self.assertIsNone(item_image.preview_height)
        self.assertEqual(item_image.list_image_srcset, '')
        self.assertEqual(item_image.detail_image_url, item_image.image.url)

    def test_item_list_prefers_the_thumbnail(self):
        item = Item.objects.create(
            title='列表小图商品', description='用于列表页验证', price='20.00',
            category=self.category, condition='九成新',
            location=self.location, seller=self.seller,
        )
        photo = ItemImage.objects.create(
            item=item, image=image_upload(name='list-cover.png'),
        )
        response = self.client.get(reverse('item_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, photo.thumbnail.url)
        self.assertNotContains(response, photo.image.url)
        self.assertContains(response, 'width="480"')
        self.assertContains(response, 'height="300"')
        self.assertContains(response, 'loading="lazy"')
        self.assertContains(response, 'decoding="async"')
    def test_item_list_offers_responsive_candidates_for_a_large_image(self):
        item = Item.objects.create(
            title='列表大图商品', description='用于列表页响应式验证', price='21.00',
            category=self.category, condition='九成新',
            location=self.location, seller=self.seller,
        )
        photo = ItemImage.objects.create(
            item=item,
            image=image_upload(name='list-large-cover.png', width=1600, height=1000),
        )
        response = self.client.get(reverse('item_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, photo.list_image_srcset)
        self.assertContains(
            response,
            'sizes="(max-width: 576px) 100vw, (max-width: 768px) 50vw, (max-width: 992px) 33vw, 25vw"',
        )
        self.assertContains(response, 'width="480"')
        self.assertContains(response, 'height="300"')
        self.assertContains(response, 'loading="lazy"')
        self.assertContains(response, 'decoding="async"')

    def test_item_detail_prefers_preview_and_keeps_original_as_a_large_candidate(self):
        item = Item.objects.create(
            title='详情大图商品', description='用于详情页响应式验证', price='22.00',
            category=self.category, condition='九成新',
            location=self.location, seller=self.seller,
        )
        photo = ItemImage.objects.create(
            item=item,
            image=image_upload(name='detail-large-cover.png', width=1600, height=1000),
        )
        response = self.client.get(reverse('item_detail', args=[item.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'src="{photo.preview.url}"')
        self.assertContains(response, photo.detail_image_srcset)
        self.assertContains(response, 'sizes="(max-width: 576px) 100vw, 60vw"')
        self.assertContains(response, 'width="960"')
        self.assertContains(response, 'height="600"')
        self.assertContains(response, 'decoding="async"')
        self.assertNotContains(response, 'loading="lazy"')




class BackfillItemThumbnailsTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='backfill-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='回填分类')
        self.location = CampusLocation.objects.create(name='回填地点')
        self.item = Item.objects.create(
            title='回填商品', description='用于回填测试', price='40.00',
            category=self.category, condition='九成新',
            location=self.location, seller=self.seller,
        )

    def make_image(self, name, **kwargs):
        return ItemImage.objects.create(
            item=self.item, image=image_upload(name=name, **kwargs),
        )

    def reset_thumbnail(self, *item_images):
        ItemImage.objects.filter(pk__in=[row.pk for row in item_images]).update(thumbnail='')

    def reset_preview(self, *item_images):
        ItemImage.objects.filter(pk__in=[row.pk for row in item_images]).update(preview='')

    def test_backfill_is_idempotent_and_records_the_run(self):
        first = self.make_image('backfill-first.png')
        second = self.make_image('backfill-second.png', width=500, height=900)
        self.reset_thumbnail(first, second)

        output = StringIO()
        call_command('backfill_item_thumbnails', stdout=output)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.thumbnail)
        self.assertTrue(second.thumbnail)

        call_command('backfill_item_thumbnails', stdout=StringIO())
        run = TaskRun.objects.filter(name='backfill_item_thumbnails').order_by('id').last()
        self.assertEqual(
            run.metrics,
            {'marked': 0, 'updated': 0, 'failed': 0, 'skipped': 0},
        )
        self.assertIn('剩余 0 条', output.getvalue())

    def test_limit_processes_the_oldest_rows_first(self):
        first = self.make_image('limit-first.png')
        second = self.make_image('limit-second.png')
        third = self.make_image('limit-third.png')
        self.reset_thumbnail(first, second, third)

        call_command('backfill_item_thumbnails', '--limit', '1', stdout=StringIO())
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertTrue(first.thumbnail)
        self.assertFalse(second.thumbnail)
        self.assertFalse(third.thumbnail)

    def test_dry_run_does_not_write_files_or_database(self):
        photo = self.make_image('dry-run.png')
        self.reset_thumbnail(photo)

        output = StringIO()
        call_command('backfill_item_thumbnails', '--dry-run', stdout=output)

        photo.refresh_from_db()
        self.assertFalse(photo.thumbnail)
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(run.note, 'dry-run')
        self.assertEqual(run.metrics, {'marked': 1})
        self.assertIn('本次待补齐商品图片缩略图、预览图或展示宽高 1 条。', output.getvalue())

    def test_a_large_historical_image_gets_both_missing_variants(self):
        photo = self.make_image('large-history.png', width=1600, height=1000)
        self.reset_thumbnail(photo)
        self.reset_preview(photo)

        call_command('backfill_item_thumbnails', stdout=StringIO())
        photo.refresh_from_db()

        self.assertTrue(photo.thumbnail.name.startswith('items/thumbnails/'))
        self.assertTrue(photo.preview.name.startswith('items/previews/'))
        self.assertEqual(
            (
                photo.image_width, photo.image_height,
                photo.thumbnail_width, photo.thumbnail_height,
                photo.preview_width, photo.preview_height,
            ),
            (1600, 1000, 480, 300, 960, 600),
        )
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(
            run.metrics,
            {'marked': 1, 'updated': 1, 'failed': 0, 'skipped': 0},
        )

    def test_an_existing_large_thumbnail_only_backfills_the_preview(self):
        photo = self.make_image('preview-history.png', width=1600, height=1000)
        thumbnail_name = photo.thumbnail.name
        self.reset_preview(photo)

        with patch('listings.models.build_item_thumbnail', side_effect=RuntimeError('boom')):
            call_command('backfill_item_thumbnails', stdout=StringIO())
        photo.refresh_from_db()

        self.assertEqual(photo.thumbnail.name, thumbnail_name)
        self.assertTrue(photo.preview.name.startswith('items/previews/'))
        self.assertEqual((photo.preview_width, photo.preview_height), (960, 600))
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(
            run.metrics,
            {'marked': 1, 'updated': 1, 'failed': 0, 'skipped': 0},
        )

    def test_preview_dimensions_can_be_backfilled_without_rebuilding_the_file(self):
        photo = self.make_image('preview-metadata.png', width=1600, height=1000)
        preview_name = photo.preview.name
        ItemImage.objects.filter(pk=photo.pk).update(preview_width=None)

        with patch('listings.models.build_item_preview', side_effect=RuntimeError('boom')):
            call_command('backfill_item_thumbnails', stdout=StringIO())
        photo.refresh_from_db()

        self.assertEqual(photo.preview.name, preview_name)
        self.assertEqual((photo.preview_width, photo.preview_height), (960, 600))
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(
            run.metrics,
            {'marked': 1, 'updated': 1, 'failed': 0, 'skipped': 0},
        )

    def test_existing_thumbnail_only_backfills_missing_dimensions(self):
        photo = self.make_image('metadata-only.png')
        thumbnail_name = photo.thumbnail.name
        ItemImage.objects.filter(pk=photo.pk).update(
            image_width=None, image_height=None,
            thumbnail_width=None, thumbnail_height=None,
        )

        with patch('listings.models.build_item_thumbnail', side_effect=RuntimeError('boom')):
            call_command('backfill_item_thumbnails', stdout=StringIO())

        photo.refresh_from_db()
        self.assertEqual(photo.thumbnail.name, thumbnail_name)
        self.assertEqual(photo.image_width, 960)
        self.assertEqual(photo.image_height, 600)
        self.assertEqual(photo.thumbnail_width, 480)
        self.assertEqual(photo.thumbnail_height, 300)
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(
            run.metrics,
            {'marked': 1, 'updated': 1, 'failed': 0, 'skipped': 0},
        )

    def test_a_broken_historical_image_does_not_stop_the_batch(self):
        valid = self.make_image('valid-history.png')
        broken = ItemImage.objects.create(
            item=self.item,
            image=SimpleUploadedFile('broken.png', b'not-a-real-image', content_type='image/png'),
        )
        self.reset_thumbnail(valid, broken)

        output = StringIO()
        call_command('backfill_item_thumbnails', stdout=output)
        valid.refresh_from_db()
        broken.refresh_from_db()
        self.assertTrue(valid.thumbnail)
        self.assertFalse(broken.thumbnail)
        run = TaskRun.objects.get(name='backfill_item_thumbnails')
        self.assertEqual(
            run.metrics,
            {'marked': 2, 'updated': 1, 'failed': 1, 'skipped': 0},
        )
        self.assertIn('失败 1 条', output.getvalue())
