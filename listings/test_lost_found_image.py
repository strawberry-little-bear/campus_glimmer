# -*- coding: utf-8 -*-
"""Pin down the single optional photo behind a lost-and-found record.

The photo is the only part of this feature that touches the filesystem, the
form and three templates at once, so these tests walk the whole path: the
variant pipeline the photo shares with item images, the three outcomes of the
form (add / replace / remove), and the pages that read the result back.
"""

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .image_processing import MAX_UPLOAD_PIXELS, THUMBNAIL_MAX_EDGE_PIXELS
from .models import (
    CampusLocation, Category, Item, ItemImage, LostFoundImage, LostFoundPost,
)
from .forms import LostFoundPostForm


def photo_upload(name='lost-photo.png', width=1600, height=1000):
    """A solid-colour PNG: deterministic, cheap, and genuinely decodable."""
    image = Image.new('RGB', (width, height), '#58a')
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


class LostFoundImageModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='lost-photo-owner', password='safe-password-123',
        )
        self.category = Category.objects.create(name='失物照片分类')
        self.location = CampusLocation.objects.create(name='失物照片地点')
        self.post = LostFoundPost.objects.create(
            reporter=self.owner,
            post_type='lost',
            title='图书馆遗失黑色水杯',
            description='黑色保温水杯，杯身有蓝色贴纸。',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=3),
            expires_at=timezone.now() + timedelta(days=10),
            identifying_features='蓝色贴纸和银色挂件',
        )

    def test_one_record_can_only_carry_one_photo(self):
        LostFoundImage.objects.create(post=self.post, image=photo_upload(name='first.png'))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LostFoundImage.objects.create(post=self.post, image=photo_upload(name='second.png'))
        self.assertEqual(LostFoundImage.objects.filter(post=self.post).count(), 1)

    def test_deleting_the_record_takes_the_photo_row_with_it(self):
        photo = LostFoundImage.objects.create(post=self.post, image=photo_upload())
        self.post.delete()
        self.assertFalse(LostFoundImage.objects.filter(pk=photo.pk).exists())

    def test_a_large_photo_gets_bounded_variants_and_dimensions(self):
        photo = LostFoundImage.objects.create(post=self.post, image=photo_upload())

        self.assertTrue(photo.thumbnail.name.startswith('lost-found/thumbnails/'))
        self.assertTrue(photo.preview.name.startswith('lost-found/previews/'))
        self.assertEqual((photo.image_width, photo.image_height), (1600, 1000))
        self.assertEqual((photo.thumbnail_width, photo.thumbnail_height), (480, 300))
        self.assertEqual((photo.preview_width, photo.preview_height), (960, 600))
        with Image.open(photo.thumbnail) as thumbnail:
            self.assertEqual(thumbnail.format, 'WEBP')
            self.assertLessEqual(max(thumbnail.size), THUMBNAIL_MAX_EDGE_PIXELS)
        self.assertEqual(photo.list_image_url, photo.thumbnail.url)
        self.assertIn(f'{photo.thumbnail.url} 480w', photo.list_image_srcset)
        self.assertEqual(photo.detail_image_url, photo.preview.url)
        self.assertIn(
            f'{photo.preview.url} 960w, {photo.image.url} 1600w',
            photo.detail_image_srcset,
        )

    def test_a_web_sized_photo_keeps_the_original_as_the_only_candidate(self):
        photo = LostFoundImage.objects.create(
            post=self.post, image=photo_upload(width=960, height=600),
        )

        self.assertFalse(photo.preview)
        self.assertIsNone(photo.preview_width)
        self.assertIsNone(photo.preview_height)
        self.assertEqual(photo.list_image_url, photo.thumbnail.url)
        self.assertEqual(photo.list_image_srcset, '')
        self.assertEqual(photo.detail_image_url, photo.image.url)
        self.assertEqual(photo.detail_image_srcset, '')

    def test_a_missing_variant_falls_back_to_the_stored_original(self):
        photo = LostFoundImage.objects.create(post=self.post, image=photo_upload())
        photo.thumbnail = None
        photo.preview = None

        self.assertEqual(photo.list_image_url, photo.image.url)
        self.assertEqual(photo.list_image_width, photo.image_width)
        self.assertEqual(photo.list_image_height, photo.image_height)
        self.assertEqual(photo.list_image_srcset, '')
        self.assertEqual(photo.detail_image_url, photo.image.url)
        self.assertEqual(photo.detail_image_width, photo.image_width)
        self.assertEqual(photo.detail_image_srcset, '')

    def test_the_previous_image_is_looked_up_on_its_own_table(self):
        """A shared mixin must not read the old file name off the item table.

        ``LostFoundImage`` and ``ItemImage`` share the variant pipeline, so the
        "did the image change?" question has to be asked of the row's own table.
        Asking it of the wrong one makes an unrelated item row decide whether a
        lost-and-found thumbnail gets rebuilt.
        """
        photo = LostFoundImage.objects.create(
            post=self.post, image=photo_upload(name='lost-photo.png'),
        )
        other_item = Item.objects.create(
            title='与失物无关的商品', description='只用来占住同一个主键。',
            price='9.00', category=self.category, condition='九成新', seller=self.owner,
        )
        ItemImage.objects.create(
            pk=photo.pk, item=other_item, image=photo_upload(name='item-photo.png'),
        )

        self.assertEqual(photo._existing_image_name(), photo.image.name)

    def test_replacing_the_photo_rebuilds_variants_and_drops_the_old_file(self):
        photo = LostFoundImage.objects.create(
            post=self.post, image=photo_upload(name='old.png'),
        )
        old_thumbnail = photo.thumbnail.name

        photo.image = photo_upload(name='new.png', width=800, height=500)
        photo.save()
        photo.refresh_from_db()

        self.assertEqual((photo.image_width, photo.image_height), (800, 500))
        self.assertNotEqual(photo.thumbnail.name, old_thumbnail)
        self.assertFalse(default_storage.exists(old_thumbnail))


class LostFoundPhotoFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='lost-photo-publisher', password='safe-password-123',
        )
        self.category = Category.objects.create(name='失物表单分类')
        self.location = CampusLocation.objects.create(name='失物表单地点')
        self.client.force_login(self.owner)

    def _payload(self, **overrides):
        now = timezone.now().replace(second=0, microsecond=0)
        payload = {
            'post_type': 'found',
            'title': '教学楼捡到一把长柄雨伞',
            'description': '在教学楼二层捡到一把深蓝色长柄雨伞，伞骨有一处掉漆。',
            'category': str(self.category.id),
            'location': str(self.location.id),
            'occurred_at': (now - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M'),
            'expires_at': (now + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'),
            'identifying_features': '伞骨掉漆，手柄缠着黑色胶带',
        }
        payload.update(overrides)
        return payload

    def test_the_form_asks_for_a_photo_but_never_requires_one(self):
        form = LostFoundPostForm()

        self.assertFalse(form.fields['photo'].required)
        self.assertIn('校园卡', form.fields['photo'].help_text)
        self.assertIn('学生证', form.fields['photo'].help_text)

    def test_publishing_with_a_photo_stores_exactly_one_row(self):
        response = self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )

        post = LostFoundPost.objects.get(reporter=self.owner)
        self.assertEqual(post.image.image_width, 1600)
        self.assertEqual(LostFoundImage.objects.filter(post=post).count(), 1)
        self.assertRedirects(response, reverse('lost_found_detail', args=[post.id]))

    def test_publishing_without_a_photo_leaves_no_row(self):
        self.client.post(reverse('new_lost_found'), self._payload())

        post = LostFoundPost.objects.get(reporter=self.owner)
        self.assertFalse(LostFoundImage.objects.filter(post=post).exists())

    def test_a_photo_beyond_the_pixel_limit_is_refused_with_the_shared_message(self):
        edge = int(MAX_UPLOAD_PIXELS ** 0.5) + 2
        response = self.client.post(
            reverse('new_lost_found'),
            self._payload(photo=photo_upload(width=edge, height=edge)),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '图片过大：单张不能超过 10 MB 或 2000 万像素。')
        self.assertFalse(LostFoundImage.objects.exists())

    def test_a_file_that_is_not_an_image_is_refused_without_leaking_details(self):
        response = self.client.post(
            reverse('new_lost_found'),
            self._payload(photo=SimpleUploadedFile(
                'broken.png', b'not an image', content_type='image/png',
            )),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '请上传一张有效的图片。')
        self.assertFalse(LostFoundImage.objects.exists())

    def test_a_photo_the_encoder_cannot_handle_reports_a_plain_message(self):
        form = LostFoundPostForm(data=self._payload(), files={'photo': photo_upload()})
        with patch(
            'listings.forms.normalize_item_image',
            side_effect=RuntimeError('boom C:/secret/path.png'),
        ):
            self.assertFalse(form.is_valid())

        self.assertEqual(
            form.errors['photo'], ['图片处理失败，请换一张清晰的图片再试。'],
        )

    def test_an_untouched_edit_keeps_the_current_photo(self):
        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)
        stored_name = post.image.image.name

        self.client.post(reverse('edit_lost_found', args=[post.id]), self._payload())

        post.image.refresh_from_db()
        self.assertEqual(post.image.image.name, stored_name)

    def test_a_new_photo_replaces_the_old_one_and_leaves_one_row(self):
        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)
        old_thumbnail = post.image.thumbnail.name

        self.client.post(
            reverse('edit_lost_found', args=[post.id]),
            self._payload(photo=photo_upload(name='replacement.png', width=800, height=500)),
        )

        photos = LostFoundImage.objects.filter(post=post)
        self.assertEqual(photos.count(), 1)
        self.assertEqual(photos.get().image_width, 800)
        self.assertNotEqual(photos.get().thumbnail.name, old_thumbnail)
        self.assertFalse(default_storage.exists(old_thumbnail))

    def test_removing_the_photo_keeps_the_record_itself(self):
        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)

        response = self.client.post(
            reverse('edit_lost_found', args=[post.id]),
            self._payload(remove_photo='on'),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(LostFoundImage.objects.filter(post=post).exists())
        self.assertTrue(LostFoundPost.objects.filter(pk=post.pk).exists())

    def test_the_remove_checkbox_only_shows_once_a_photo_exists(self):
        new_form = self.client.get(reverse('new_lost_found'))
        self.assertNotContains(new_form, 'name="remove_photo"')

        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)
        edit_form = self.client.get(reverse('edit_lost_found', args=[post.id]))

        self.assertContains(edit_form, 'name="remove_photo"')
        self.assertContains(edit_form, post.image.list_image_url)

    def test_the_list_page_renders_the_thumbnail(self):
        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)

        response = self.client.get(reverse('lost_found_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, post.image.list_image_url)
        self.assertContains(response, 'width="480"')
        self.assertContains(response, 'height="300"')
        self.assertContains(response, 'loading="lazy"')

    def test_the_detail_page_prefers_the_preview_and_explains_the_photo(self):
        self.client.post(
            reverse('new_lost_found'), self._payload(photo=photo_upload()),
        )
        post = LostFoundPost.objects.get(reporter=self.owner)

        response = self.client.get(reverse('lost_found_detail', args=[post.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'src="{post.image.preview.url}"')
        self.assertContains(response, post.image.detail_image_srcset)
        self.assertContains(response, '仅用于辅助辨认物品')
        self.assertContains(response, 'decoding="async"')
