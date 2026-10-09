from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import BrowsingHistory, Category, CampusLocation, Favorite, Item, Order
from .supply_lifecycle import (
    ENGAGEMENT_WINDOW_DAYS,
    MAX_REFRESH_PER_MONTH,
    STALE_DAYS,
    build_supply_lifecycle,
)


class SupplyLifecycleTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='supply-seller', password='safe-password-123')
        self.other = User.objects.create_user(username='supply-seller-2', password='safe-password-123')
        self.buyer = User.objects.create_user(username='supply-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='供给侧分类')
        self.location = CampusLocation.objects.create(name='北门')

    def make_item(self, seller=None, **kwargs):
        defaults = {
            'title': '供给侧商品',
            'description': '用于测试供给侧生命周期聚合',
            'price': '42.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': seller or self.seller,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def aged(self, item, days):
        stamp = timezone.now() - timedelta(days=days)
        Item.objects.filter(pk=item.pk).update(created_at=stamp)
        item.refresh_from_db()
        return item

    def refreshed(self, item, days_ago=0, count=1):
        stamp = timezone.now() - timedelta(days=days_ago)
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=stamp, refresh_count=count,
        )
        item.refresh_from_db()
        return item

    def test_empty_platform_reports_zeroes_and_no_recommendations(self):
        data = build_supply_lifecycle(30)

        summary = data['summary']
        self.assertEqual(summary['available_items'], 0)
        self.assertEqual(summary['attention_items'], 0)
        self.assertEqual(summary['attention_share'], 0)
        self.assertEqual(summary['sellers_with_listings'], 0)
        # 没有数据时必须留空，而不是输出一堆通用建议。
        self.assertEqual(data['recommendations'], [])
        self.assertEqual(data['level_rows'][0]['count'], 0)

    def test_level_distribution_matches_the_seller_facing_rules(self):
        self.make_item(title='新鲜')
        self.aged(self.make_item(title='放缓'), 21)
        self.aged(self.make_item(title='沉底'), 45)
        # 已售出的商品不算“在售”，也不该出现在分布里。
        self.make_item(title='已售', status='sold')

        levels = {row['key']: row['count'] for row in build_supply_lifecycle(30)['level_rows']}

        self.assertEqual(levels['fresh'], 1)
        self.assertEqual(levels['slowing'], 1)
        self.assertEqual(levels['stale'], 1)
        # 只要在售口径，archived 不参与分布。
        self.assertNotIn('archived', levels)
        self.assertEqual(sum(levels.values()), 3)

    def test_expired_listings_are_excluded_from_the_available_supply(self):
        item = self.make_item()
        Item.objects.filter(pk=item.pk).update(expires_at=timezone.now() - timedelta(hours=2))

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['available_items'], 0)
        self.assertEqual(summary['sellers_with_listings'], 0)

    def test_attention_share_counts_only_attention_levels(self):
        self.aged(self.make_item(title='沉底'), 60)
        self.make_item(title='新鲜')

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['available_items'], 2)
        self.assertEqual(summary['attention_items'], 1)
        self.assertEqual(summary['attention_share'], 50.0)

    def test_stale_listing_without_engagement_is_flagged_as_unengaged(self):
        self.aged(self.make_item(title='沉底无互动'), 60)

        summary = build_supply_lifecycle(30)['summary']
        self.assertEqual(summary['stale_unengaged'], 1)

        # 有人浏览过就不再算“无人维护”。
        browsed = self.make_item(title='沉底有互动')
        self.aged(browsed, 60)
        BrowsingHistory.objects.create(user=self.buyer, item=browsed, view_count=1)

        summary = build_supply_lifecycle(30)['summary']
        self.assertEqual(summary['stale_unengaged'], 1)

    def test_engagement_outside_the_window_does_not_rescue_a_stale_listing(self):
        item = self.aged(self.make_item(), 60)
        history = BrowsingHistory.objects.create(user=self.buyer, item=item, view_count=1)
        # 互动发生在窗口之外，说明它现在同样没人看。
        BrowsingHistory.objects.filter(pk=history.pk).update(
            last_viewed_at=timezone.now() - timedelta(days=ENGAGEMENT_WINDOW_DAYS + 3),
        )

        self.assertEqual(build_supply_lifecycle(30)['summary']['stale_unengaged'], 1)

    def test_favourite_and_order_also_count_as_engagement(self):
        favoured = self.aged(self.make_item(title='被收藏'), 60)
        Favorite.objects.create(user=self.buyer, item=favoured)
        ordered = self.aged(self.make_item(title='被预约'), 60)
        Order.objects.create(
            item=ordered, buyer=self.buyer, seller=self.seller, agreed_price='42.00',
        )

        self.assertEqual(build_supply_lifecycle(30)['summary']['stale_unengaged'], 0)

    def test_seller_coverage_measures_sellers_with_something_to_maintain(self):
        # 这位卖家名下所有商品都需要处理。
        self.aged(self.make_item(title='沉底'), 60)
        self.aged(self.make_item(title='放缓'), 30)
        # 这位卖家只有新鲜商品，算“跟上了”。
        self.make_item(seller=self.other, title='新鲜')

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['sellers_with_listings'], 2)
        self.assertEqual(summary['sellers_keeping_up'], 1)
        self.assertEqual(summary['seller_coverage'], 50.0)
        self.assertEqual(summary['neglected_sellers'], 1)

    def test_users_without_listings_never_dilute_seller_coverage(self):
        self.make_item()
        for index in range(5):
            User.objects.create_user(username=f'bystander-{index}', password='safe-password-123')

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['sellers_with_listings'], 1)
        self.assertEqual(summary['sellers_keeping_up'], 1)
        self.assertEqual(summary['seller_coverage'], 100.0)

    def test_exhausted_refresh_quota_is_reported_separately(self):
        self.refreshed(self.make_item(), count=MAX_REFRESH_PER_MONTH)
        self.refreshed(self.make_item(seller=self.other), count=1)

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['exhausted_sellers'], 1)
        rows = {row['label']: row for row in build_supply_lifecycle(30)['seller_load_rows']}
        # 两位卖家各 1 件在管，都应落在最小的桶里。
        self.assertEqual(rows['仅 1 件在管']['seller_count'], 2)
        self.assertEqual(rows['仅 1 件在管']['exhausted_seller_count'], 1)

    def test_seller_load_buckets_cover_every_listing_exactly_once(self):
        self.make_item(title='a')
        for index in range(2):
            self.make_item(seller=self.other, title=f'b-{index}')
        heavy = User.objects.create_user(username='supply-heavy', password='safe-password-123')
        for index in range(9):
            self.make_item(seller=heavy, title=f'c-{index}')

        rows = build_supply_lifecycle(30)['seller_load_rows']

        self.assertEqual(sum(row['seller_count'] for row in rows), 3)
        self.assertEqual(sum(row['active_count'] for row in rows), 12)
        labels = [row['label'] for row in rows]
        self.assertIn('仅 1 件在管', labels)
        self.assertIn('2-3 件在管', labels)
        self.assertIn('9 件以上在管', labels)

    def test_period_counters_ignore_activity_outside_the_period(self):
        old = self.make_item(title='旧商品')
        Item.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=120),
            last_refreshed_at=timezone.now() - timedelta(days=100),
        )
        self.make_item(title='新商品')

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['period_new'], 1)
        self.assertEqual(summary['period_refreshes'], 0)
        self.assertEqual(summary['period_refreshed_sellers'], 0)

    def test_period_refresh_counters_count_sellers_not_events(self):
        first = self.refreshed(self.make_item(title='一'), days_ago=2, count=2)
        second = self.refreshed(self.make_item(title='二'), days_ago=1, count=1)

        summary = build_supply_lifecycle(30)['summary']

        self.assertEqual(summary['period_refreshes'], 2)
        self.assertEqual(summary['period_refreshed_sellers'], 1)

    def test_recommendations_appear_only_when_a_threshold_is_crossed(self):
        # 1 件新鲜商品：任何阈值都不该触发。
        self.make_item()
        self.assertEqual(build_supply_lifecycle(30)['recommendations'], [])

        # 20% 以上在售商品需要处理时，才会给出曝光建议。
        self.aged(self.make_item(title='沉底'), 60)
        recommendations = build_supply_lifecycle(30)['recommendations']
        self.assertTrue(any('曝光' in text for text in recommendations))

    def test_recommendations_mention_neglected_sellers_when_they_cluster(self):
        for index in range(3):
            seller = User.objects.create_user(
                username=f'neglected-{index}', password='safe-password-123',
            )
            self.aged(self.make_item(seller=seller, title=f'沉底-{index}'), 60)

        recommendations = build_supply_lifecycle(30)['recommendations']

        self.assertTrue(any('一次性触达' in text for text in recommendations))

    def test_recommendation_for_unengaged_supply_names_the_count(self):
        for index in range(10):
            self.aged(self.make_item(title=f'沉底-{index}'), 60)

        recommendations = build_supply_lifecycle(30)['recommendations']

        self.assertTrue(any('10 件' in text for text in recommendations))
