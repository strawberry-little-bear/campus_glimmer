# -*- coding: utf-8 -*-
"""为「免费领取申请队列」钉住口径。

`GiftApplication` 的 `applicant_note`、`decided_at`、`meeting_location` 一直在写，
读路径只有后台那一列。这个模块把它们读出来，只报告不改写。这些测试要钉住的是：

1. 只统计已经处理完的申请，`pending` 不计入等待分布，但单独数出来。
2. 撤回单独一档，不和「未选中」混在一起。
3. 申请说明只报填写率和填 / 没填两组的选中率对照，不读正文。
4. 两组各自不足 12 条时对照整组不给方向，差值不满 8 个百分点视为噪声。
5. 队列长度按商品聚合，选中率按申请聚合，只收到一份申请的商品单独一档。
6. 队列长度分档的分母是商品数，不是申请数。
7. 处理时长按小时和天分档，负值被夹到零。
8. 窗口按 `decided_at` 取，不按 `created_at`。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .gift_application_queue import (
    CATEGORY_LIST_LIMIT, DECISION_BUCKETS, ITEM_LIST_LIMIT,
    MIN_GROUP_APPLICATIONS, MIN_MEASURED_APPLICATIONS, NOISE_POINTS,
    QUEUE_BUCKETS, build_gift_application_queue,
)
from .models import Category, CampusLocation, GiftApplication, Item


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class GiftApplicationQueueTests(TestCase):
    def setUp(self):
        self.seller = make_user('gift-seller')
        self.category = Category.objects.create(name='免费领取测试分类')
        self.location = CampusLocation.objects.create(
            name='免费领取测试地点', building='免费领取测试楼', is_public=True,
        )
        self._item_seq = 0
        self._applicant_seq = 0

    def make_item(self, title='免费测试商品'):
        self._item_seq += 1
        return Item.objects.create(
            title=f'{title}{self._item_seq}', description='用于申请队列统计',
            price='0.00', trade_mode='free', category=self.category,
            condition='良好', seller=self.seller, location=self.location,
            status='available',
        )

    def make_application(
        self, *, item=None, status='rejected', decided_seconds=3600,
        note='', submitted_days_ago=2,
    ):
        """建一条申请，处理间隔按秒给，可以指定状态和申请说明。"""
        self._applicant_seq += 1
        applicant = make_user(f'gift-applicant-{self._applicant_seq}')
        item = item or self.make_item()
        created_at = timezone.now() - timedelta(days=submitted_days_ago)
        row = GiftApplication.objects.create(
            item=item,
            applicant=applicant,
            meeting_location=self.location,
            applicant_note=note,
            status=status,
            decided_at=created_at + timedelta(seconds=decided_seconds),
        )
        # created_at 是 auto_now_add，不能直接在 create 里给，只能建完再改。
        row.created_at = created_at
        row.save(update_fields=['created_at'])
        return row

    def test_only_decided_applications_are_measured(self):
        self.make_application(status='selected', note='想要')
        self.make_application(status='rejected')
        # pending 没有处理时刻，不该进任何等待分布。
        GiftApplication.objects.create(
            item=self.make_item(), applicant=make_user('gift-pending'),
            meeting_location=self.location,
        )

        report = build_gift_application_queue(days=30)
        self.assertEqual(report['stats']['measured_count'], 2)
        # pending 单独数出来，不丢也不混。
        self.assertEqual(report['pending_count'], 1)

    def test_withdrawn_is_its_own_bucket(self):
        self.make_application(status='withdrawn')
        report = build_gift_application_queue(days=30)
        self.assertEqual(report['stats']['withdrawn_count'], 1)
        # 撤回不和「未选中」混在一起：选中率的分母里它是独立的一类。
        self.assertEqual(report['stats']['selected_rate'], 0)
        self.assertEqual(report['stats']['withdrawn_rate'], 100)
        self.assertEqual(len(report['withdrawn_rows']), 1)

    def test_note_fill_rate_counts_only_applications_with_text(self):
        self.make_application(note='  想要，可以自取  ')
        self.make_application(note='')

        report = build_gift_application_queue(days=30)
        self.assertEqual(report['note']['filled_count'], 1)
        self.assertEqual(report['note']['empty_count'], 1)
        self.assertEqual(report['note']['fill_rate'], 50)

    def test_note_comparison_is_silent_when_a_group_is_too_small(self):
        # 两组各自不足 12 条时，对照整组不给方向——选中率是稀有事件，两组各要这么
        # 多条，差距才落得到噪声带外面。
        for index in range(MIN_GROUP_APPLICATIONS - 1):
            self.make_application(note='想要')
        for index in range(MIN_GROUP_APPLICATIONS + 3):
            self.make_application(note='')

        report = build_gift_application_queue(days=30)
        self.assertFalse(report['note']['is_comparable'])
        self.assertIsNone(report['note']['difference'])
        self.assertEqual(report['note']['direction'], '')

    def test_note_comparison_reports_difference_once_both_groups_are_big_enough(self):
        # 两组都够大，差值够大，才给方向。
        for index in range(MIN_GROUP_APPLICATIONS):
            self.make_application(note='想要', status='selected')
        for index in range(MIN_GROUP_APPLICATIONS):
            self.make_application(note='', status='rejected')

        report = build_gift_application_queue(days=30)
        self.assertTrue(report['note']['is_comparable'])
        self.assertEqual(report['note']['difference'], 100)
        self.assertEqual(report['note']['direction'], '填了说明的更容易被选中')

    def test_note_comparison_calls_a_small_difference_noise(self):
        # 差值不满 8 个百分点视为噪声，不给方向。两组的选中率要挨得够近，测的才
        # 是「差距小」而不是「某一组根本没被选中」。
        for index in range(MIN_GROUP_APPLICATIONS):
            self.make_application(
                note='想要', status='selected' if index < 3 else 'rejected',
            )
        for index in range(MIN_GROUP_APPLICATIONS + 3):
            self.make_application(
                note='', status='selected' if index < 4 else 'rejected',
            )

        report = build_gift_application_queue(days=30)
        self.assertTrue(report['note']['is_comparable'])
        self.assertLess(abs(report['note']['difference']), NOISE_POINTS)
        self.assertEqual(report['note']['direction'], '两组差距落在噪声带内')

    def test_queue_length_is_measured_per_item_not_per_application(self):
        # 一件商品八份申请，队列长度是一，不是八。
        item = self.make_item()
        for index in range(8):
            self.make_application(item=item)
        other = self.make_item()
        self.make_application(item=other, status='selected')

        report = build_gift_application_queue(days=30)
        self.assertEqual(report['stats']['measured_count'], 9)
        # 分母是商品数：两件商品。
        self.assertEqual(report['stats']['item_count'], 2)
        # 队长的排在前面。
        self.assertEqual(report['item_rows'][0]['queue_length'], 8)
        self.assertTrue(report['item_rows'][0]['is_crowded'])
        # 只收到一份申请的商品单独一档，选中率不参与平均。
        self.assertFalse(report['item_rows'][1]['is_crowded'])

    def test_queue_buckets_cover_every_item(self):
        item = self.make_item()
        for index in range(5):
            self.make_application(item=item)
        pair = self.make_item()
        for index in range(2):
            self.make_application(item=pair)
        self.make_application(item=self.make_item())

        report = build_gift_application_queue(days=30)
        buckets = {row['label']: row['count'] for row in report['queue_rows']}
        self.assertEqual(buckets['五份及以上'], 1)
        self.assertEqual(buckets['两到四份'], 1)
        self.assertEqual(buckets['一份申请'], 1)
        self.assertEqual(buckets['没有申请'], 0)

    def test_decision_buckets_are_mutually_exclusive_and_clamped_at_zero(self):
        # submitted_days_ago 要跟着 decided_seconds 走，否则 decided_at 落到未来，
        # 被 decided_before=now 整条挡掉——那是窗口本身的行为，不是分档的行为。
        self.make_application(decided_seconds=3600, submitted_days_ago=2)
        self.make_application(decided_seconds=2 * 86400, submitted_days_ago=5)
        self.make_application(decided_seconds=4 * 86400, submitted_days_ago=10)
        self.make_application(decided_seconds=8 * 86400, submitted_days_ago=20)
        # 负值（处理早于提交）被夹到零，归到最短的一档。
        self.make_application(decided_seconds=-600, submitted_days_ago=2)

        report = build_gift_application_queue(days=30)
        buckets = {row['label']: row['count'] for row in report['stats']['rows']}
        self.assertEqual(buckets['当天内'], 2)
        self.assertEqual(buckets['一到三天'], 1)
        self.assertEqual(buckets['三到七天'], 1)
        self.assertEqual(buckets['超过七天'], 1)
        self.assertEqual(
            sum(row['count'] for row in report['stats']['rows']),
            report['stats']['measured_count'],
        )

    def test_window_follows_decided_at_not_created_at(self):
        # 一条 45 天前提交、20 天后才被选中的申请，decided_at 落在本周期里，该被
        # 收进来。按 created_at 取窗口会把它整条漏掉——而它等的正是窗口内的这批人。
        self.make_application(
            status='selected', decided_seconds=20 * 86400, submitted_days_ago=45,
        )
        # 两条 decided_at 早于窗口起点的申请，不该进来。
        self.make_application(
            status='rejected', decided_seconds=3600, submitted_days_ago=60,
        )
        self.make_application(
            status='rejected', decided_seconds=3600, submitted_days_ago=90,
        )

        report = build_gift_application_queue(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)
        self.assertEqual(report['item_rows'][0]['queue_length'], 1)

    def test_empty_report_is_explicit(self):
        report = build_gift_application_queue(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['stats']['measured_count'], 0)
        self.assertEqual(report['stats']['median_label'], '—')
        self.assertEqual(report['item_rows'], [])
        self.assertEqual(report['category_rows'], [])
        self.assertTrue(report['stats']['is_small_sample'])

    def test_small_sample_is_flagged(self):
        for index in range(MIN_MEASURED_APPLICATIONS - 1):
            self.make_application()
        report = build_gift_application_queue(days=30)
        self.assertTrue(report['stats']['is_small_sample'])
        self.assertEqual(report['min_measured_applications'], MIN_MEASURED_APPLICATIONS)
        self.assertEqual(report['min_group_applications'], MIN_GROUP_APPLICATIONS)

    def test_category_rows_group_by_item_category(self):
        item = self.make_item()
        for index in range(3):
            self.make_application(item=item, status='selected')

        report = build_gift_application_queue(days=30)
        self.assertEqual(len(report['category_rows']), 1)
        row = report['category_rows'][0]
        self.assertEqual(row['category_name'], '免费领取测试分类')
        self.assertEqual(row['applications'], 3)
        self.assertEqual(row['selected_count'], 3)
        self.assertEqual(row['selected_rate'], 100)

    def test_row_limits_are_reported(self):
        for index in range(ITEM_LIST_LIMIT + 5):
            self.make_application()
        report = build_gift_application_queue(days=30)
        self.assertEqual(len(report['item_rows']), ITEM_LIST_LIMIT)
        self.assertGreater(report['item_row_total'], ITEM_LIST_LIMIT)
        self.assertLessEqual(
            len(report['category_rows']), CATEGORY_LIST_LIMIT,
        )
        self.assertLessEqual(
            len(report['withdrawn_rows']), report['withdrawn_row_limit'],
        )

    def test_period_choices_are_the_only_accepted_windows(self):
        for days in (7, 30, 90, 365):
            report = build_gift_application_queue(days=days)
            self.assertEqual(report['period_days'], days)
        # 不认识的窗口退回 30 天，而不是当 0 天用。
        report = build_gift_application_queue(days=3)
        self.assertEqual(report['period_days'], 30)
