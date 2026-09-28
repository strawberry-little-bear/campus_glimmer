from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .meeting_scheduling import recommend_meeting_locations, recommend_meeting_times
from .models import CampusLocation, Category, Item, MeetingAppointment, Order


class MeetingRecommendationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='buyer', password='safe-password-123')
        self.category = Category.objects.create(name='推荐测试分类')
        self.item_location = CampusLocation.objects.create(
            name='图书馆大厅', building='东校区', is_public=True,
        )
        self.history_location = CampusLocation.objects.create(
            name='体育馆门口', building='西校区', is_public=True,
        )
        self.order = Order.objects.create(
            item=Item.objects.create(
                title='推荐测试商品', description='用于测试交付推荐', price='20.00',
                category=self.category, location=self.item_location,
                condition='良好', seller=self.seller,
            ),
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.item_location,
            agreed_price='20.00',
            status='confirmed',
        )

    def test_location_recommendation_explains_item_and_pair_history(self):
        history_item = Item.objects.create(
            title='历史商品', description='历史交付', price='10.00',
            category=self.category, location=self.history_location,
            condition='良好', seller=self.seller,
        )
        history_order = Order.objects.create(
            item=history_item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.history_location,
            agreed_price='10.00',
            status='completed',
        )
        start_at = timezone.now() + timedelta(days=2)
        MeetingAppointment.objects.create(
            order=history_order,
            proposed_by=self.seller,
            location=self.history_location,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            status='confirmed',
        )

        recommendations = recommend_meeting_locations(order=self.order)

        self.assertEqual(recommendations[0].location, self.item_location)
        self.assertTrue(any('商品发布地点' in reason for reason in recommendations[0].reasons))
        history_recommendation = next(
            recommendation for recommendation in recommendations
            if recommendation.location == self.history_location
        )
        self.assertEqual(history_recommendation.pair_history_count, 1)
        self.assertTrue(any('你们曾在这里完成 1 次交付' in reason for reason in history_recommendation.reasons))

    def test_time_recommendation_skips_both_party_conflicts(self):
        now = timezone.make_aware(datetime(2026, 9, 28, 10, 0))
        conflict_item = Item.objects.create(
            title='冲突商品', description='已有预约', price='15.00',
            category=self.category, location=self.item_location,
            condition='良好', seller=self.seller,
        )
        conflict_order = Order.objects.create(
            item=conflict_item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.item_location,
            agreed_price='15.00',
            status='meeting',
        )
        conflict_start = now.replace(hour=17)
        MeetingAppointment.objects.create(
            order=conflict_order,
            proposed_by=self.seller,
            location=self.item_location,
            start_at=conflict_start,
            end_at=conflict_start + timedelta(hours=1),
            status='confirmed',
        )

        recommendations = recommend_meeting_times(order=self.order, now=now, days=2, limit=20)

        self.assertTrue(recommendations)
        self.assertTrue(all(
            recommendation.start_at != conflict_start
            for recommendation in recommendations
        ))
        self.assertTrue(all(recommendation.start_input.endswith(':00') for recommendation in recommendations))
        self.assertTrue(all(any('没有其他交付预约冲突' in reason for reason in recommendation.reasons) for recommendation in recommendations))

    def test_order_detail_displays_recommendations(self):
        self.client.login(username='buyer', password='safe-password-123')

        response = self.client.get(reverse('order_detail', args=[self.order.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '智能建议')
        self.assertContains(response, '图书馆大厅')
        self.assertContains(response, '推荐空闲时段')
