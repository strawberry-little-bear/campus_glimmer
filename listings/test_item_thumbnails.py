# -*- coding: utf-8 -*-
"""Pin down list thumbnails and their incremental historical backfill."""

from io import BytesIO, StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from .image_processing import THUMBNAIL_MAX_EDGE_PIXELS, build_item_thumbnail
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
        with Image.open(result) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertEqual(thumbnail.size, (180, 120))

    def test_exif_orientation_and_alpha_are_preserved(self):
        result = build_item_thumbnail(
            image_upload(width=800, height=400, format='JPEG', orientation=6),
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
        with Image.open(item_image.thumbnail) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertLessEqual(max(thumbnail.size), THUMBNAIL_MAX_EDGE_PIXELS)
        self.assertEqual(item_image.list_image_url, item_image.thumbnail.url)

    def test_replacing_the_original_rebuilds_and_removes_the_old_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item, image=image_upload(name='first-cover.png'),
        )
        old_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(name='second-cover.png', width=700, height=900)
        item_image.save()
        item_image.refresh_from_db()

        self.assertNotEqual(item_image.thumbnail.name, old_thumbnail)
        self.assertFalse(item_image.thumbnail.storage.exists(old_thumbnail))
        with Image.open(item_image.thumbnail) as thumbnail:
            self.assertEqual(thumbnail.size, (373, 480))

    def test_partial_update_persists_the_rebuilt_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item, image=image_upload(name='partial-first.png'),
        )
        old_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(name='partial-second.png', width=600, height=1000)
        item_image.save(update_fields=['image'])
        item_image.refresh_from_db()

        self.assertTrue(item_image.thumbnail)
        self.assertNotEqual(item_image.thumbnail.name, old_thumbnail)

    def test_failed_generation_keeps_the_original_readable_and_clears_stale_thumbnail(self):
        item_image = ItemImage.objects.create(
            item=self.item, image=image_upload(name='stale-cover.png'),
        )
        stale_thumbnail = item_image.thumbnail.name
        item_image.image = image_upload(name='fallback-cover.png')
        with patch('listings.models.build_item_thumbnail', side_effect=RuntimeError('boom')):
            item_image.save()
        item_image.refresh_from_db()

        self.assertFalse(item_image.thumbnail)
        self.assertFalse(item_image.thumbnail.storage.exists(stale_thumbnail))
        self.assertEqual(item_image.list_image_url, item_image.image.url)

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
        self.assertEqual(run.metrics, {'marked': 0, 'failed': 0, 'skipped': 0})
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
        self.assertIn('待回填商品图片缩略图 1 条', output.getvalue())

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
            run.metrics, {'marked': 2, 'failed': 1, 'skipped': 0},
        )
        self.assertIn('失败 1 条', output.getvalue())
