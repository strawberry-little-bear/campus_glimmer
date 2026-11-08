from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation, Category, CommunityContribution, DemandPost, DemandReopenEvent,
    DemandResponse, GiftApplication, Item, Notification, Order,
)


reopen_backfill_migration = import_module('listings.migrations.0084_demand_reopen_event')


class DemandResponseFlowTests(TestCase):
    def setUp(self):
        self.requester = User.objects.create_user(
            username='demand-requester', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='demand-seller', password='safe-password-123',
        )
        self.other_seller = User.objects.create_user(
            username='demand-other-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='响应测试分类')
        self.location = CampusLocation.objects.create(name='响应测试地点')
        self.demand = DemandPost.objects.create(
            requester=self.requester,
            title='求购高等数学教材',
            description='需要一本适合本学期使用的教材',
            category=self.category,
            location=self.location,
            min_price='20.00',
            max_price='80.00',
        )
        self.item = Item.objects.create(
            seller=self.seller,
            title='高等数学教材九成新',
            description='课本保存良好，适合本学期使用',
            category=self.category,
            location=self.location,
            condition='9成新',
            price='50.00',
        )

    def _respond(self, user=None, item=None, message='可以在图书馆门口交付。'):
        self.client.login(
            username=(user or self.seller).username,
            password='safe-password-123',
        )
        return self.client.post(
            reverse('respond_to_demand', args=[self.demand.id, (item or self.item).id]),
            {'message': message},
        )

    def test_seller_can_respond_once_and_requester_can_confirm(self):
        response = self._respond()
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        demand_response = DemandResponse.objects.get(demand=self.demand, item=self.item)
        self.assertEqual(demand_response.status, 'pending')
        self.assertGreaterEqual(demand_response.match_score, 2)
        self.assertIn('地点一致', demand_response.match_reason)
        self.assertTrue(Notification.objects.filter(
            recipient=self.requester, kind='demand_response', demand=self.demand,
        ).exists())

        duplicate = self._respond()
        self.assertRedirects(duplicate, reverse('demand_detail', args=[self.demand.id]))
        self.assertEqual(DemandResponse.objects.filter(demand=self.demand, item=self.item).count(), 1)

        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        accepted = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(accepted, reverse('demand_detail', args=[self.demand.id]))
        demand_response.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(demand_response.status, 'accepted')
        self.assertIsNotNone(demand_response.accepted_at)
        self.assertEqual(self.demand.status, 'fulfilled')
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller, kind='demand_response', item=self.item,
            title='你的求购响应已被确认',
        ).exists())
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.seller, kind='demand_helped',
            ).count(),
            1,
        )
        self.assertEqual(
            CommunityContribution.objects.get(
                user=self.seller, kind='demand_helped',
            ).points,
            8,
        )

        duplicate = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(duplicate, reverse('demand_detail', args=[self.demand.id]))
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.seller, kind='demand_helped',
            ).count(),
            1,
        )

    def test_accepting_one_response_defers_other_pending_responses(self):
        other_item = Item.objects.create(
            seller=self.other_seller,
            title='高等数学教材另一本',
            description='同类教材',
            category=self.category,
            location=self.location,
            condition='8成新',
            price='40.00',
        )
        first = self._respond()
        self.assertEqual(first.status_code, 302)
        second = self._respond(user=self.other_seller, item=other_item, message='我也有一本。')
        self.assertEqual(second.status_code, 302)
        first_response = DemandResponse.objects.get(item=self.item, demand=self.demand)
        second_response = DemandResponse.objects.get(item=other_item, demand=self.demand)

        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        self.client.post(reverse('review_demand_response', args=[first_response.id, 'accept']))
        second_response.refresh_from_db()
        self.assertEqual(second_response.status, 'deferred')
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_seller, kind='demand_response', item=other_item,
            title='求购响应暂时停用',
        ).exists())

    def _accept_response(self):
        self._respond()
        demand_response = DemandResponse.objects.get(
            demand=self.demand, item=self.item,
        )
        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        accepted = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertEqual(accepted.status_code, 302)
        demand_response.refresh_from_db()
        self.assertEqual(demand_response.status, 'accepted')
        return demand_response

    def test_accepted_response_owner_can_continue_to_order(self):
        demand_response = self._accept_response()
        detail_url = reverse('demand_detail', args=[self.demand.id])
        self.assertContains(
            self.client.get(detail_url),
            f'?demand_response={demand_response.id}',
        )

        order_url = reverse('create_order', args=[self.item.id])
        response = self.client.post(order_url, {
            'meeting_location': self.location.id,
            'buyer_note': '按求购约定在图书馆见',
            'demand_response': demand_response.id,
        })

        order = Order.objects.get(item=self.item)
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.assertEqual(order.buyer, self.requester)
        self.assertEqual(order.seller, self.seller)
        self.assertEqual(order.demand_response, demand_response)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'reserved')
        self.assertContains(self.client.get(detail_url), '这次匹配已创建交易预约')
        self.assertContains(self.client.get(detail_url), '查看交易')

        duplicate = self.client.post(order_url, {
            'meeting_location': self.location.id,
            'buyer_note': '重复提交',
            'demand_response': demand_response.id,
        })
        self.assertRedirects(duplicate, reverse('order_detail', args=[order.id]))
        self.assertEqual(Order.objects.filter(item=self.item).count(), 1)

    def test_response_handoff_rejects_another_user(self):
        demand_response = self._accept_response()
        outsider = User.objects.create_user(
            username='demand-outsider', password='safe-password-123',
        )
        self.client.force_login(outsider)
        response = self.client.post(
            reverse('create_order', args=[self.item.id]),
            {
                'meeting_location': self.location.id,
                'demand_response': demand_response.id,
            },
        )

        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertFalse(Order.objects.filter(item=self.item).exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'available')

    def test_response_handoff_rejects_a_different_item(self):
        demand_response = self._accept_response()
        other_item = Item.objects.create(
            seller=self.seller,
            title='另一件教材',
            description='不是这条响应对应的商品',
            category=self.category,
            location=self.location,
            condition='8成新',
            price='35.00',
        )
        response = self.client.post(
            reverse('create_order', args=[other_item.id]),
            {
                'meeting_location': self.location.id,
                'demand_response': demand_response.id,
            },
        )

        self.assertRedirects(response, reverse('item_detail', args=[other_item.id]))
        self.assertFalse(Order.objects.filter(item=other_item).exists())
        other_item.refresh_from_db()
        self.assertEqual(other_item.status, 'available')

    def test_non_matching_item_cannot_respond(self):
        other_location = CampusLocation.objects.create(name='其他响应地点')
        item = Item.objects.create(
            seller=self.seller,
            title='完全不同的商品',
            description='不符合求购条件',
            category=self.category,
            location=other_location,
            condition='全新',
            price='50.00',
        )
        response = self._respond(item=item)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DemandResponse.objects.filter(demand=self.demand, item=item).exists())

    def _make_other_response(self):
        other_item = Item.objects.create(
            seller=self.other_seller,
            title='另一本可用教材',
            description='符合求购条件，可以继续响应',
            category=self.category,
            location=self.location,
            condition='8成新',
            price='45.00',
        )
        self._respond(user=self.other_seller, item=other_item, message='我也有一本。')
        return DemandResponse.objects.get(demand=self.demand, item=other_item)

    def _make_confirmed_item_unavailable(self):
        self.item.status = 'sold'
        self.item.save(update_fields=['status', 'updated_at'])

    def _reopen(self, demand_response):
        return self.client.post(reverse('reopen_demand_response', args=[
            self.demand.id, demand_response.id,
        ]))

    def test_unavailable_accepted_item_can_reopen_and_restore_deferred_response(self):
        deferred = self._make_other_response()
        accepted = self._accept_response()
        self.client.post(reverse('review_demand_response', args=[accepted.id, 'accept']))
        deferred.refresh_from_db()
        self.assertEqual(deferred.status, 'deferred')
        self._make_confirmed_item_unavailable()
        detail_url = reverse('demand_detail', args=[self.demand.id])
        self.assertContains(self.client.get(detail_url), '重新选择响应')

        response = self._reopen(accepted)
        self.assertRedirects(response, detail_url)
        accepted.refresh_from_db()
        deferred.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'superseded')
        self.assertIsNotNone(accepted.accepted_at)
        self.assertIsNotNone(accepted.superseded_at)
        self.assertEqual(deferred.status, 'pending')
        self.assertEqual(self.demand.status, 'active')
        self.assertEqual(self.demand.reopen_count, 1)
        self.assertIsNotNone(self.demand.last_reopened_at)
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller,
            title='求购已重新选择响应',
            demand=self.demand,
        ).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.other_seller,
            title='你的求购响应恢复待处理',
            demand=self.demand,
        ).exists())

    def test_reopen_keeps_explicitly_rejected_response_rejected(self):
        rejected = self._make_other_response()
        self._respond()
        accepted = DemandResponse.objects.get(demand=self.demand, item=self.item)
        self.client.logout()
        self.client.login(username='demand-requester', password='safe-password-123')
        self.client.post(reverse('review_demand_response', args=[rejected.id, 'reject']))
        self.client.post(reverse('review_demand_response', args=[accepted.id, 'accept']))
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, 'rejected')
        self._make_confirmed_item_unavailable()

        self._reopen(accepted)
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, 'rejected')

    def test_reopen_is_idempotent(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self._reopen(accepted)
        self._reopen(accepted)
        self.demand.refresh_from_db()
        self.assertEqual(self.demand.reopen_count, 1)
        self.assertEqual(Notification.objects.filter(
            recipient=self.seller,
            title='求购已重新选择响应',
            demand=self.demand,
        ).count(), 1)
        self.assertEqual(DemandReopenEvent.objects.filter(demand=self.demand).count(), 1)

    def test_reopen_event_snapshots_the_replaced_response(self):
        accepted = self._accept_response()
        self.item.trade_mode = 'borrow'
        self.item.save(update_fields=['trade_mode', 'updated_at'])
        self._make_confirmed_item_unavailable()

        self._reopen(accepted)
        accepted.refresh_from_db()
        event = DemandReopenEvent.objects.get(demand=self.demand)

        self.assertEqual(event.response_id, accepted.id)
        self.assertEqual(event.previous_item_id, self.item.id)
        self.assertEqual(event.previous_item_title, self.item.title)
        self.assertEqual(event.previous_trade_mode, 'borrow')
        self.assertEqual(event.previous_accepted_at, accepted.accepted_at)
        self.assertEqual(event.reopened_at, accepted.superseded_at)
        self.assertEqual(event.reopen_count_after, 1)
        self.assertEqual(event.source, 'runtime')

    def test_event_snapshot_is_immutable(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self._reopen(accepted)
        event = DemandReopenEvent.objects.get(demand=self.demand)
        event.previous_item_title = '试图改写历史'

        with self.assertRaisesMessage(ValidationError, '不可变历史'):
            event.save()

    def test_database_rejects_duplicate_reopen_ordinal(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self._reopen(accepted)
        accepted.refresh_from_db()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DemandReopenEvent.objects.create(
                    demand=self.demand,
                    response=accepted,
                    previous_item=self.item,
                    previous_item_title=self.item.title,
                    previous_trade_mode=self.item.trade_mode,
                    previous_accepted_at=accepted.accepted_at,
                    reopened_at=timezone.now(),
                    reopen_count_after=1,
                    source='runtime',
                )

    def test_backfill_migration_rebuilds_legacy_superseded_history(self):
        now = timezone.now()
        first_accepted_at = now - timedelta(days=6)
        first_reopened_at = now - timedelta(days=4)
        second_accepted_at = now - timedelta(days=3)
        second_reopened_at = now - timedelta(days=1)
        first = DemandResponse.objects.create(
            demand=self.demand,
            item=self.item,
            responder=self.seller,
            status='superseded',
        )
        second_item = Item.objects.create(
            seller=self.other_seller,
            title='高等数学教材备用本',
            description='另一本同类教材',
            category=self.category,
            location=self.location,
            condition='8成新',
            price='45.00',
        )
        second = DemandResponse.objects.create(
            demand=self.demand,
            item=second_item,
            responder=self.other_seller,
            status='superseded',
        )
        DemandResponse.objects.filter(pk=first.pk).update(
            accepted_at=first_accepted_at,
            superseded_at=first_reopened_at,
            updated_at=first_reopened_at,
        )
        DemandResponse.objects.filter(pk=second.pk).update(
            accepted_at=second_accepted_at,
            superseded_at=second_reopened_at,
            updated_at=second_reopened_at,
        )

        reopen_backfill_migration.backfill_reopen_events(apps, None)

        events = DemandReopenEvent.objects.filter(demand=self.demand).order_by(
            'reopened_at',
        )
        self.assertEqual(
            [event.reopen_count_after for event in events],
            [1, 2],
        )
        self.assertEqual({event.source for event in events}, {'backfill'})
        first_event = events.get(response=first)
        self.assertEqual(first_event.previous_item_id, self.item.id)
        self.assertEqual(first_event.previous_item_title, self.item.title)
        self.assertEqual(first_event.previous_accepted_at, first_accepted_at)
        self.assertEqual(first_event.reopened_at, first_reopened_at)

    def test_reopen_rolls_back_when_event_cannot_be_written(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()

        with patch(
            'listings.views.DemandReopenEvent.objects.create',
            side_effect=RuntimeError('event write failed'),
        ):
            with self.assertRaisesMessage(RuntimeError, 'event write failed'):
                self._reopen(accepted)

        accepted.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')
        self.assertIsNone(accepted.superseded_at)
        self.assertEqual(self.demand.status, 'fulfilled')
        self.assertEqual(self.demand.reopen_count, 0)
        self.assertFalse(DemandReopenEvent.objects.filter(demand=self.demand).exists())

    def test_available_item_cannot_be_reopened(self):
        accepted = self._accept_response()
        response = self._reopen(accepted)
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        accepted.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')
        self.assertEqual(self.demand.status, 'fulfilled')
        self.assertEqual(self.demand.reopen_count, 0)
        self.assertFalse(DemandReopenEvent.objects.filter(demand=self.demand).exists())

    def test_non_owner_cannot_reopen_response(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self.client.force_login(self.seller)
        response = self._reopen(accepted)
        self.assertEqual(response.status_code, 404)
        accepted.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')

    def test_response_with_order_cannot_be_reopened(self):
        accepted = self._accept_response()
        Order.objects.create(
            item=self.item,
            buyer=self.requester,
            seller=self.seller,
            demand_response=accepted,
            meeting_location=self.location,
            agreed_price='50.00',
        )
        self.item.status = 'reserved'
        self.item.save(update_fields=['status', 'updated_at'])

        self._reopen(accepted)
        accepted.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')
        self.assertEqual(self.demand.status, 'fulfilled')

    def test_response_with_gift_application_cannot_be_reopened(self):
        accepted = self._accept_response()
        GiftApplication.objects.create(
            item=self.item,
            applicant=self.requester,
            meeting_location=self.location,
            demand_response=accepted,
        )
        self.item.status = 'reserved'
        self.item.save(update_fields=['status', 'updated_at'])

        self._reopen(accepted)
        accepted.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')
        self.assertEqual(self.demand.status, 'fulfilled')

    def test_same_item_can_respond_again_after_reopen_without_overwriting_history(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self._reopen(accepted)
        accepted.refresh_from_db()
        self.assertEqual(accepted.status, 'superseded')

        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])
        self._respond()
        responses = DemandResponse.objects.filter(demand=self.demand, item=self.item)
        self.assertEqual(responses.count(), 2)
        self.assertEqual(responses.filter(status='superseded').count(), 1)
        self.assertEqual(responses.filter(status='pending').count(), 1)

    def test_expired_demand_cannot_be_reopened(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        self.demand.expires_at = timezone.now() - timedelta(minutes=1)
        self.demand.save(update_fields=['expires_at', 'updated_at'])

        self._reopen(accepted)
        accepted.refresh_from_db()
        self.demand.refresh_from_db()
        self.assertEqual(accepted.status, 'accepted')
        self.assertEqual(self.demand.status, 'fulfilled')

    def test_my_demand_list_only_flags_a_replaceable_accepted_item(self):
        accepted = self._accept_response()
        self._make_confirmed_item_unavailable()
        list_url = reverse('my_demands')

        list_response = self.client.get(list_url)
        card = next(
            demand for demand in list_response.context['demands']
            if demand.pk == self.demand.pk
        )
        self.assertTrue(card.can_reopen_demand_response)
        self.assertEqual(card.reopen_item_title, self.item.title)
        self.assertContains(list_response, '已确认的商品失效')
        self.assertContains(list_response, '进入详情重新选择响应')

        detail_response = self.client.get(reverse('demand_detail', args=[self.demand.id]))
        self.assertTrue(detail_response.context['can_reopen_demand_response'])
        self.assertContains(detail_response, '重新选择响应')

        self.item.status = 'available'
        self.item.save(update_fields=['status', 'updated_at'])
        response = self.client.get(list_url)
        self.assertNotContains(response, '已确认的商品失效')
        self.assertFalse(response.context['demands'][0].can_reopen_demand_response)

        self.item.status = 'sold'
        self.item.save(update_fields=['status', 'updated_at'])
        self.demand.expires_at = timezone.now() - timedelta(minutes=1)
        self.demand.save(update_fields=['expires_at', 'updated_at'])
        self.assertNotContains(self.client.get(list_url), '已确认的商品失效')

        self.demand.expires_at = None
        self.demand.save(update_fields=['expires_at', 'updated_at'])
        Order.objects.create(
            item=self.item,
            buyer=self.requester,
            seller=self.seller,
            demand_response=accepted,
            meeting_location=self.location,
            agreed_price='50.00',
        )
        self.assertNotContains(self.client.get(list_url), '已确认的商品失效')

        Order.objects.filter(demand_response=accepted).delete()
        GiftApplication.objects.create(
            item=self.item,
            applicant=self.requester,
            meeting_location=self.location,
            demand_response=accepted,
        )
        self.assertNotContains(self.client.get(list_url), '已确认的商品失效')

    def test_my_demand_list_query_count_does_not_grow_with_cards(self):
        self._accept_response()
        self._make_confirmed_item_unavailable()
        list_url = reverse('my_demands')

        with CaptureQueriesContext(connection) as one_card:
            self.client.get(list_url)
        for index in range(11):
            DemandPost.objects.create(
                requester=self.requester,
                title=f'不相关求购 {index}',
                description='用来把第一页铺满',
                category=self.category,
                location=self.location,
            )
        with CaptureQueriesContext(connection) as twelve_cards:
            self.client.get(list_url)

        self.assertEqual(len(one_card), len(twelve_cards))

    def test_only_requester_can_review_response(self):
        self._respond()
        demand_response = DemandResponse.objects.get(demand=self.demand, item=self.item)
        self.client.logout()
        self.client.login(username='demand-seller', password='safe-password-123')
        response = self.client.post(
            reverse('review_demand_response', args=[demand_response.id, 'accept']),
        )
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        demand_response.refresh_from_db()
        self.assertEqual(demand_response.status, 'pending')
