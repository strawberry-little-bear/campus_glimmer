# -*- coding: utf-8 -*-
"""Pin down item photo normalization before it enters media storage."""

from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from .forms import ItemImageForm, ItemImageFormSet
from .image_processing import (
    MAX_EDGE_PIXELS, MAX_UPLOAD_BYTES, MAX_UPLOAD_PIXELS,
    OPTIMIZE_THRESHOLD_BYTES, ItemImageLimitError, normalize_item_image,
)
from .models import CampusLocation, Category, Item


def make_upload(
    name='photo.png', width=120, height=80, format='PNG', mode='RGB',
    orientation=None,
):
    image = Image.new(mode, (width, height), '#3aa')
    if mode == 'RGBA':
        # A partially transparent pixel proves the alpha channel is retained;
        # fully opaque WebP output may legally report RGB.
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


class NormalizeItemImageTests(TestCase):
    def test_small_upload_is_returned_without_reencoding(self):
        upload = make_upload(width=160, height=90)
        result = normalize_item_image(upload)
        self.assertIs(result, upload)

    def test_large_edge_is_downscaled_and_converted_to_webp(self):
        result = normalize_item_image(make_upload(width=2600, height=1400))
        with Image.open(result) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertLessEqual(max(image.size), MAX_EDGE_PIXELS)
            self.assertAlmostEqual(
                image.size[0] / image.size[1], 2600 / 1400, places=2,
            )
        self.assertTrue(result.name.endswith('.webp'))
        self.assertLess(result.size, 2600 * 1400 * 3)

    def test_large_file_size_triggers_reencode_even_with_small_dimensions(self):
        upload = make_upload(width=100, height=80)
        upload.size = OPTIMIZE_THRESHOLD_BYTES + 1
        result = normalize_item_image(upload)
        with Image.open(result) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertEqual(image.size, (100, 80))

    def test_file_hard_limit_is_checked_before_raster_is_loaded(self):
        upload = make_upload(width=20, height=20)
        upload.size = MAX_UPLOAD_BYTES + 1
        with self.assertRaises(ItemImageLimitError):
            normalize_item_image(upload)

    def test_pixel_hard_limit_is_checked_before_full_decode(self):
        # White PNG compresses well, so this tests the pixel guard without
        # allocating a giant uploaded body.
        upload = make_upload(width=5000, height=4001)
        self.assertGreater(upload.size, 0)
        self.assertLess(upload.size, 1024 * 1024)
        with self.assertRaises(ItemImageLimitError):
            normalize_item_image(upload)

    def test_exif_portrait_orientation_is_applied_when_reencoding(self):
        upload = make_upload(
            name='portrait.jpg', width=2400, height=1200,
            format='JPEG', orientation=6,
        )
        result = normalize_item_image(upload)
        with Image.open(result) as image:
            self.assertGreater(image.size[1], image.size[0])
            self.assertLessEqual(max(image.size), MAX_EDGE_PIXELS)

    def test_png_alpha_survives_webp_reencoding(self):
        result = normalize_item_image(
            make_upload(width=1800, height=120, mode='RGBA'),
        )
        with Image.open(result) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertIn('A', image.getbands())

    def test_declared_limits_match_the_design(self):
        self.assertEqual(MAX_UPLOAD_BYTES, 10 * 1024 * 1024)
        self.assertEqual(MAX_UPLOAD_PIXELS, 20_000_000)
        self.assertEqual(OPTIMIZE_THRESHOLD_BYTES, 1536 * 1024)
        self.assertEqual(MAX_EDGE_PIXELS, 1600)


class ItemImageFormNormalizationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='image-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='图片处理分类')
        self.location = CampusLocation.objects.create(name='图片处理地点')
        self.item = Item.objects.create(
            title='图片处理商品', description='用于图片表单测试',
            price='50.00', category=self.category, condition='良好',
            location=self.location, seller=self.seller,
        )

    def test_form_replaces_oversized_upload_before_save(self):
        form = ItemImageForm(
            files={'image': make_upload(width=2400, height=1300)},
        )
        self.assertTrue(form.is_valid(), form.errors)
        cleaned = form.cleaned_data['image']
        self.assertTrue(cleaned.name.endswith('.webp'))
        with Image.open(cleaned) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertLessEqual(max(image.size), MAX_EDGE_PIXELS)

    def test_inline_formset_uses_the_same_guard_on_edit_path(self):
        upload = make_upload(width=2400, height=1300)
        management_data = {
            'images-TOTAL_FORMS': '1',
            'images-INITIAL_FORMS': '0',
            'images-MIN_NUM_FORMS': '0',
            'images-MAX_NUM_FORMS': '5',
        }
        formset = ItemImageFormSet(
            data=management_data,
            files={'images-0-image': upload},
            instance=self.item,
        )
        self.assertTrue(formset.is_valid(), formset.non_form_errors())
        formset.save()
        stored = self.item.images.get()
        self.assertTrue(stored.image.name.endswith('.webp'))
        with stored.image.open('rb') as handle:
            with Image.open(handle) as image:
                self.assertEqual(image.format, 'WEBP')
                self.assertLessEqual(max(image.size), MAX_EDGE_PIXELS)

    def test_hard_limit_error_is_user_friendly(self):
        upload = make_upload(width=20, height=20)
        upload.size = MAX_UPLOAD_BYTES + 1
        form = ItemImageForm(files={'image': upload})
        self.assertFalse(form.is_valid())
        self.assertIn('单张不能超过 10 MB 或 2000 万像素', str(form.errors['image']))

    def test_processing_failure_does_not_leak_internal_details(self):
        upload = make_upload(width=30, height=20)
        with patch(
            'listings.forms.normalize_item_image',
            side_effect=RuntimeError('E:\\secret\\decoder.dll failed'),
        ):
            form = ItemImageForm(files={'image': upload})
            self.assertFalse(form.is_valid())
        message = str(form.errors['image'])
        self.assertIn('图片处理失败', message)
        self.assertNotIn('secret', message)
        self.assertNotIn('decoder.dll', message)

    def test_upload_form_discloses_limits_and_accept_attribute(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse('new_item'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '单张不超过 10 MB 或 2000 万像素')
        self.assertContains(response, 'accept="image/*"')

    def test_invalid_image_still_gets_field_level_validation(self):
        upload = SimpleUploadedFile(
            'not-an-image.png', b'this is not an image', content_type='image/png',
        )
        form = ItemImageForm(files={'image': upload})
        self.assertFalse(form.is_valid())
        self.assertIn('请上传一张有效的图片', str(form.errors['image']))
