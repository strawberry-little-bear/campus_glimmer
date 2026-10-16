# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campaign_phase_review import build_campaign_phase_review
from .models import BrowsingHistory, CampusCampaign, Category, Favorite, Item, Order


class CampaignPhaseReviewTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='campaign-phase-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题阶段复盘分类')
        self.user_index = 0

    def _unique_user(self, prefix):
        self.user_index += 1
        return User.objects.create_user(
            username=f'{prefix}-{self.user_index}', password='safe-password-123',
        )

    def _campaign(self, title, *, days_ago, duration_days=None, slug=None):
        starts_at = self.now - timedelta(days=days_ago)
        ends_at = None
        if duration_days is not None:
            ends_at = starts_at + timedelta(days=duration_days)
        return CampusCampaign.objects.create(
            title=title,
            slug=slug or f'phase-review-{CampusCampaign.objects.count() + 1}',
            starts_at=starts_at,
            ends_at=ends_at,
        )

    def _item(self, campaign, title, created_at):
        item = Item.objects.create(
            title=title,
            description='用于校园专题分阶段复盘测试',
            price='20.00',
            category=self.category,
            condition='良好',
            seller=self.seller,
            campaign=campaign,
        )
        Item.objects.filter(pk=item.pk).update(created_at=created_at)
        item.refresh_from_db()
        return item

    def _view(self, item, viewed_at):
        history = BrowsingHistory.objects.create(
            user=self._unique_user('phase-viewer'), item=item,
        )
        BrowsingHistory.objects.filter(pk=history.pk).update(
            last_viewed_at=viewed_at,
        )
        return history

    def _favorite(self, item, created_at):
        favorite = Favorite.objects.create(
            user=self._unique_user('phase-favorite'), item=item,
        )
        Favorite.objects.filter(pk=favorite.pk).update(created_at=created_at)
        return favorite

    def _order(self, item, created_at, *, status='completed'):
        order = Order.objects.create(
            item=item,
            buyer=self._unique_user('phase-buyer'),
            seller=self.seller,
            agreed_price='20.00',
            status=status,
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        return order

    def _phase_map(self, row):
        return {phase['key']: phase for phase in row['phases']}

    def test_only_closed_campaigns_are_reviewed(self):
        closed = self._campaign(
            '已经结束的专题', days_ago=20, duration_days=10,
            slug='phase-closed',
        )
        self._campaign(
            '仍在进行的专题', days_ago=5, duration_days=10,
            slug='phase-live',
        )
        self._campaign('无结束时间专题', days_ago=5, slug='phase-endless')
        self._campaign(
            '尚未开始的专题', days_ago=-1, duration_days=10,
            slug='phase-future',
        )

        report = build_campaign_phase_review(now=self.now)

        self.assertEqual(report['summary']['closed_campaign_count'], 1)
        self.assertEqual(report['summary']['reviewed_campaign_count'], 1)
        self.assertEqual(report['summary']['open_campaign_count'], 2)
        self.assertEqual([row['id'] for row in report['rows']], [closed.id])
        self.assertFalse(report['cross_campaign']['has_conclusion'])

    def test_each_event_family_uses_its_business_timestamp_at_half_open_boundaries(self):
        campaign = self._campaign(
            '边界专题', days_ago=30, duration_days=30,
            slug='phase-boundaries',
        )
        boundaries = [
            campaign.starts_at + timedelta(days=10),
            campaign.starts_at + timedelta(days=20),
        ]
        timestamps = [
            campaign.starts_at,
            boundaries[0],
            boundaries[1],
            campaign.ends_at,
        ]
        items = [
            self._item(campaign, f'阶段边界商品-{index}', occurred_at)
            for index, occurred_at in enumerate(timestamps)
        ]
        for item, occurred_at in zip(items, timestamps):
            self._view(item, occurred_at)
            self._favorite(item, occurred_at)
            self._order(item, occurred_at)

        report = build_campaign_phase_review(now=self.now)
        row = report['rows'][0]
        phases = self._phase_map(row)

        for key in ('opening', 'middle', 'closing'):
            self.assertEqual(phases[key]['new_items'], 1)
            self.assertEqual(phases[key]['views'], 1)
            self.assertEqual(phases[key]['favorites'], 1)
            self.assertEqual(phases[key]['orders'], 1)
            self.assertEqual(phases[key]['completed_orders'], 1)
        self.assertEqual(report['summary']['new_items'], 3)
        self.assertEqual(report['summary']['views'], 3)
        self.assertEqual(report['summary']['favorites'], 3)
        self.assertEqual(report['summary']['orders'], 3)
        self.assertEqual(report['summary']['completed_orders'], 3)

    def test_rates_and_per_100_day_intensity_use_exact_phase_lengths(self):
        campaign = self._campaign(
            '转化与强度专题', days_ago=30, duration_days=30,
            slug='phase-rates',
        )
        phase_starts = [
            campaign.starts_at + timedelta(days=1),
            campaign.starts_at + timedelta(days=11),
            campaign.starts_at + timedelta(days=21),
        ]
        items = [
            self._item(campaign, f'阶段转化商品-{index}', starts_at)
            for index, starts_at in enumerate(phase_starts)
        ]
        for item in items:
            for offset in range(4):
                self._view(item, item.created_at + timedelta(hours=offset + 1))

        opening_times = (
            phase_starts[0],
            phase_starts[0] + timedelta(hours=1),
            phase_starts[0] + timedelta(hours=2),
        )
        for occurred_at in opening_times:
            self._order(items[0], occurred_at)
        self._order(items[0], phase_starts[0] + timedelta(hours=3), status='cancelled')
        for offset in range(1):
            self._order(items[1], phase_starts[1] + timedelta(hours=offset))
        for offset in range(2):
            self._order(items[1], phase_starts[1] + timedelta(hours=offset + 4), status='cancelled')
        for offset in range(3):
            self._order(items[2], phase_starts[2] + timedelta(hours=offset), status='cancelled')

        report = build_campaign_phase_review(now=self.now)
        phases = self._phase_map(report['rows'][0])

        self.assertEqual(phases['opening']['views'], 4)
        self.assertEqual(phases['middle']['views'], 4)
        self.assertEqual(phases['closing']['views'], 4)
        self.assertEqual(phases['opening']['orders'], 4)
        self.assertEqual(phases['middle']['orders'], 3)
        self.assertEqual(phases['closing']['orders'], 3)
        self.assertEqual(phases['opening']['completed_orders'], 3)
        self.assertEqual(phases['middle']['completed_orders'], 1)
        self.assertEqual(phases['closing']['completed_orders'], 0)
        self.assertEqual(phases['opening']['view_to_order_rate'], 100.0)
        self.assertEqual(phases['middle']['view_to_order_rate'], 75.0)
        self.assertEqual(phases['closing']['view_to_order_rate'], 75.0)
        self.assertEqual(phases['opening']['completion_rate'], 75.0)
        self.assertEqual(phases['middle']['completion_rate'], 33.3)
        self.assertEqual(phases['closing']['completion_rate'], 0.0)
        for key in ('opening', 'middle', 'closing'):
            self.assertEqual(phases[key]['days'], 10.0)
            self.assertEqual(phases[key]['views_per_100_days'], 40.0)
        self.assertEqual(phases['opening']['orders_per_100_days'], 40.0)
        self.assertEqual(phases['middle']['orders_per_100_days'], 30.0)
        self.assertEqual(phases['closing']['orders_per_100_days'], 30.0)
        self.assertEqual(report['rows'][0]['order_peak_key'], 'opening')
        self.assertNotEqual(report['rows'][0]['order_peak_key'], 'closing')

    def test_thin_phase_and_exact_tie_do_not_claim_a_peak(self):
        thin = self._campaign(
            '薄样本专题', days_ago=40, duration_days=30,
            slug='phase-thin',
        )
        thin_items = [
            self._item(
                thin,
                f'薄样本商品-{index}',
                thin.starts_at + timedelta(days=index * 10 + 1),
            )
            for index in range(3)
        ]
        for index, count in enumerate((2, 3, 3)):
            for offset in range(count):
                self._order(
                    thin_items[index],
                    thin_items[index].created_at + timedelta(hours=offset),
                )

        tied = self._campaign(
            '并列专题', days_ago=20, duration_days=10,
            slug='phase-tied',
        )
        tied_items = [
            self._item(
                tied,
                f'并列商品-{index}',
                tied.starts_at + timedelta(days=index * 3 + 1),
            )
            for index in range(3)
        ]
        for item in tied_items:
            for offset in range(3):
                self._order(item, item.created_at + timedelta(hours=offset))

        report = build_campaign_phase_review(now=self.now)
        rows = {row['slug']: row for row in report['rows']}

        self.assertFalse(rows['phase-thin']['has_order_sample'])
        self.assertEqual(rows['phase-thin']['rhythm_label'], '样本不足，只报数字')
        self.assertIsNone(rows['phase-thin']['order_peak_key'])
        self.assertEqual(rows['phase-tied']['rhythm_label'], '预约节奏均衡')
        self.assertIsNone(rows['phase-tied']['order_peak_key'])

    def test_cross_campaign_summary_counts_peaks_without_ranking(self):
        profiles = (
            ('phase-peak-opening', (6, 3, 3)),
            ('phase-peak-middle', (3, 6, 3)),
            ('phase-peak-closing', (3, 3, 6)),
        )
        for index, (slug, counts) in enumerate(profiles):
            campaign = self._campaign(
                f'高峰位置专题-{index}', days_ago=30 + index, duration_days=20,
                slug=slug,
            )
            for phase_index, count in enumerate(counts):
                observed_at = campaign.starts_at + timedelta(
                    days=phase_index * (20 / 3) + 1,
                )
                item = self._item(
                    campaign, f'高峰商品-{index}-{phase_index}', observed_at,
                )
                for offset in range(count):
                    self._order(item, observed_at + timedelta(hours=offset))

        report = build_campaign_phase_review(now=self.now)
        summary = report['cross_campaign']
        counts = {item['key']: item['count'] for item in summary['peak_counts']}

        self.assertTrue(summary['has_conclusion'])
        self.assertEqual(summary['eligible_campaign_count'], 3)
        self.assertEqual(counts, {'opening': 1, 'middle': 1, 'closing': 1})
        self.assertIn('不做专题排名', summary['conclusion'])

    def test_report_does_not_expose_user_or_item_level_identifiers(self):
        campaign = self._campaign(
            '隐私边界专题', days_ago=10, duration_days=8,
            slug='phase-privacy',
        )
        item = self._item(
            campaign, '不应出现在复盘里的商品标题', campaign.starts_at + timedelta(days=1),
        )
        self._view(item, campaign.starts_at + timedelta(days=1))
        self._favorite(item, campaign.starts_at + timedelta(days=1))
        self._order(item, campaign.starts_at + timedelta(days=1))

        report_text = repr(build_campaign_phase_review(now=self.now))

        self.assertNotIn(self.seller.username, report_text)
        self.assertNotIn('不应出现在复盘里的商品标题', report_text)
        self.assertIn('隐私边界专题', report_text)

    def test_limits_to_the_eight_latest_closed_campaigns(self):
        campaigns = []
        for index in range(10):
            campaigns.append(self._campaign(
                f'历史专题-{index}', days_ago=20 + index, duration_days=10,
                slug=f'phase-limit-{index}',
            ))

        report = build_campaign_phase_review(now=self.now)

        self.assertEqual(report['summary']['closed_campaign_count'], 10)
        self.assertEqual(report['summary']['reviewed_campaign_count'], 8)
        self.assertTrue(report['is_truncated'])
        self.assertEqual(
            report['rows'][0]['id'],
            max(campaigns, key=lambda campaign: campaign.ends_at).id,
        )
        self.assertEqual(report['rows'][-1]['id'], campaigns[7].id)

    def test_page_and_csv_share_the_same_read_only_panel(self):
        campaign = self._campaign(
            '导出闭环专题', days_ago=12, duration_days=10,
            slug='phase-export',
        )
        item = self._item(
            campaign, '导出闭环商品', campaign.starts_at + timedelta(days=1),
        )
        self._order(item, campaign.starts_at + timedelta(days=1))
        admin = User.objects.create_user(
            username='phase-review-admin', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(admin)

        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))
        csv_text = export.content.decode('utf-8-sig')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '校园专题分阶段复盘')
        self.assertContains(page, '导出闭环专题')
        self.assertIn('校园专题分阶段复盘', csv_text)
        self.assertIn(
            '专题,阶段,开始时间,结束时间,阶段天数,新增商品,浏览,收藏,预约,完成交易,'
            '浏览转预约（%）,预约完成率（%）,预约/百天,浏览/百天,新增/百天',
            csv_text,
        )
        self.assertIn('只读复盘', csv_text)
