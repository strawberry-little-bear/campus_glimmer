from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .models import CampusLocation, Category, Item, ItemImage


class BulkListingImageQueueTests(TestCase):
    """毕业季批量发布后的短期补图队列。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='image-seller', password='safe-password-123',
        )
        self.other_seller = User.objects.create_user(
            username='other-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='毕业季闲置')
        self.location = CampusLocation.objects.create(name='西门快递点')
        self.url = reverse('bulk_listing_image_queue')

    def make_item(self, title, *, seller=None, status='available', expires_at=None):
        return Item.objects.create(
            seller=seller or self.seller,
            title=title,
            description=f'{title}的描述',
            trade_mode='sale',
            price=Decimal('12.00'),
            deposit_amount=Decimal('0.00'),
            borrow_days=7,
            category=self.category,
            location=self.location,
            condition='良好',
            status=status,
            expires_at=expires_at,
        )

    def add_image(self, item):
        buffer = BytesIO()
        Image.new('RGB', (2, 2), '#48c').save(buffer, format='PNG')
        return ItemImage.objects.create(
            item=item,
            image=SimpleUploadedFile(
                f'{item.pk}.png', buffer.getvalue(), content_type='image/png',
            ),
        )

    def set_queue(self, items, *, seller_id=None, published_at=None):
        session = self.client.session
        session['listings:bulk_listing_image_queue'] = {
            'seller_id': self.seller.id if seller_id is None else seller_id,
            'item_ids': [item.id for item in items],
            'published_at': (
                published_at or timezone.now() - timedelta(minutes=3)
            ).isoformat(),
        }
        session.save()

    def bulk_payload(self, titles):
        payload = {
            'category': self.category.id,
            'location': self.location.id,
            'campaign': '',
            'expires_at': '',
            'form-TOTAL_FORMS': str(len(titles)),
            'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '20',
        }
        for index, title in enumerate(titles):
            payload.update({
                f'form-{index}-title': title,
                f'form-{index}-description': f'{title}的描述',
                f'form-{index}-trade_mode': 'sale',
                f'form-{index}-price': '12.00',
                f'form-{index}-deposit_amount': '',
                f'form-{index}-borrow_days': '',
                f'form-{index}-condition': '良好',
            })
        return payload

    def test_bulk_publish_stores_latest_batch_and_enters_queue(self):
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches') as saved_mock,
              patch('listings.views.notify_demand_matches') as demand_mock):
            response = self.client.post(
                reverse('bulk_create_items'),
                self.bulk_payload(['数据结构教材', '台灯']),
            )

        self.assertRedirects(response, self.url)
        state = self.client.session['listings:bulk_listing_image_queue']
        self.assertEqual(state['seller_id'], self.seller.id)
        self.assertEqual(
            state['item_ids'],
            list(Item.objects.order_by('id').values_list('id', flat=True)),
        )
        self.assertEqual(saved_mock.call_count, 2)
        self.assertEqual(demand_mock.call_count, 2)

        queue = self.client.get(self.url)
        self.assertEqual(queue.status_code, 200)
        my_items = self.client.get(reverse('my_items'))
        self.assertContains(my_items, '本批次补图')
        self.assertContains(queue, '本批次发布')
        self.assertContains(queue, '仍待补图')
        self.assertContains(queue, '数据结构教材')
        self.assertContains(queue, '台灯')

    def test_anonymous_queue_page_is_redirected_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_queue_only_lists_current_sellers_displayable_items_without_images(self):
        now = timezone.now()
        pending = self.make_item('待补图教材')
        reserved = self.make_item('已预订台灯', status='reserved')
        with_image = self.make_item('已有图片折叠桌')
        self.add_image(with_image)
        sold = self.make_item('已售出计算器', status='sold')
        expired = self.make_item(
            '已过期风扇', expires_at=now - timedelta(minutes=1),
        )
        other_sellers = self.make_item('其他卖家商品', seller=self.other_seller)
        self.set_queue([
            pending, reserved, with_image, sold, expired, other_sellers,
        ])
        self.client.force_login(self.seller)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['pending_items']), [pending, reserved])
        self.assertEqual(response.context['published_count'], 6)
        self.assertEqual(response.context['completed_count'], 1)
        self.assertEqual(response.context['pending_count'], 2)
        self.assertEqual(response.context['not_actionable_count'], 3)
        self.assertEqual(response.context['progress_percent'], 33)
        self.assertContains(response, pending.title)
        self.assertContains(response, reserved.title)
        self.assertNotContains(response, with_image.title)
        self.assertNotContains(response, sold.title)
        self.assertNotContains(response, expired.title)
        self.assertNotContains(response, other_sellers.title)

    def test_deleted_item_leaves_the_todo_without_breaking_progress(self):
        pending = self.make_item('保留的教材')
        deleted = self.make_item('删除的台灯')
        self.set_queue([pending, deleted])
        deleted.delete()
        self.client.force_login(self.seller)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['pending_items']), [pending])
        self.assertEqual(response.context['not_actionable_count'], 1)
        self.assertEqual(response.context['progress_percent'], 0)

    def test_queue_from_another_seller_is_rejected(self):
        item = self.make_item('第一位卖家的教材')
        self.client.force_login(self.other_seller)
        self.set_queue([item], seller_id=self.seller.id)

        response = self.client.get(self.url)

        self.assertRedirects(response, reverse('my_items'))

    def test_invalid_or_missing_session_state_redirects_to_my_items(self):
        self.client.force_login(self.seller)

        missing = self.client.get(self.url)
        self.assertRedirects(missing, reverse('my_items'))

        session = self.client.session
        session['listings:bulk_listing_image_queue'] = {
            'seller_id': self.seller.id,
            'item_ids': [1, 'bad-id'],
            'published_at': timezone.now().isoformat(),
        }
        session.save()
        invalid = self.client.get(self.url)
        self.assertRedirects(invalid, reverse('my_items'))

    def test_next_bulk_publish_replaces_the_previous_queue(self):
        first = self.make_item('第一批教材')
        self.set_queue([first])
        self.client.force_login(self.seller)

        with (patch('listings.views.notify_saved_search_matches'),
              patch('listings.views.notify_demand_matches')):
            response = self.client.post(
                reverse('bulk_create_items'), self.bulk_payload(['第二批台灯']),
            )

        self.assertRedirects(response, self.url)
        state = self.client.session['listings:bulk_listing_image_queue']
        second = Item.objects.get(title='第二批台灯')
        self.assertEqual(state['item_ids'], [second.id])
        page = self.client.get(self.url)
        self.assertEqual(list(page.context['pending_items']), [second])
        self.assertNotContains(page, first.title)

    def test_completed_queue_shows_closed_state_and_keeps_single_edit_entry(self):
        item = self.make_item('已经补图的教材')
        self.add_image(item)
        self.set_queue([item])
        self.client.force_login(self.seller)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '这一批不用再补了')
        self.assertNotContains(response, '去补图')
