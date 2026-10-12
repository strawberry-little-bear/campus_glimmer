# -*- coding: utf-8 -*-
"""为「校园身份认证的最后一步花了多久」钉住口径。

`CampusVerification` 的 `token_issued_at` 和 `verified_at` 一直在写，差值从来没
被算过。这个模块把它算出来，只报告不改写。这些测试要钉住的是：

1. 只统计已经认证成功的记录，`pending` 和 `unverified` 不计入耗时分布，
   但 `pending` 单独数出来。
2. 耗时按分钟和小时分档，边界值落在正确的一档，不按秒报。
3. 窗口按 `verified_at` 取，不按 `token_issued_at`。
4. 按域名分组，且分组排序把样本量小的域名压下去。
5. 「超过一天」是慢的判定门槛，慢的清单按耗时倒序。
6. 样本不足时要说出来，不假装有方向。
7. 负耗时（认证早于发邮件）被夹到零。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import CampusVerification
from .campus_verification_friction import (
    DOMAIN_LIST_LIMIT, ELAPSED_BUCKETS, MIN_MEASURED_VERIFICATIONS,
    SLOW_LIST_LIMIT, build_campus_verification_friction,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class CampusVerificationFrictionTests(TestCase):
    def setUp(self):
        self.domain = 'university.edu.cn'
        self._seq = 0

    def make_verification(
        self, *, elapsed_seconds=600, verified_days_ago=1, status='verified',
        domain_name='示例大学', send_token=True,
    ):
        """建一条认证记录，耗时按秒给，可以指定状态和是否发过邮件。"""
        self._seq += 1
        user = make_user(f'student-{self._seq}')
        verified_at = timezone.now() - timedelta(days=verified_days_ago)
        row = CampusVerification.objects.create(
            user=user,
            campus_email=f'student{self._seq}@{self.domain}',
            domain_name=domain_name,
            status=status,
            verified_at=verified_at if status == 'verified' else None,
        )
        # token_issued_at 不是 auto_now_add，可以直接在 create 里给。
        row.token_issued_at = (
            verified_at - timedelta(seconds=elapsed_seconds) if send_token else None
        )
        row.save(update_fields=['token_issued_at'])
        return row

    def test_only_verified_records_are_measured(self):
        self.make_verification(elapsed_seconds=600)
        # pending 和 unverified 都不该进耗时分布。
        self.make_verification(status='pending')
        self.make_verification(status='unverified', send_token=False)

        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)
        # pending 单独数出来，不丢也不混。
        self.assertEqual(report['pending_count'], 1)

    def test_unverified_without_token_is_not_measured(self):
        # 压根没发过邮件的记录，token_issued_at 是空的，谈不上耗时。
        self.make_verification(send_token=False)
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['measured_count'], 0)
        self.assertFalse(report['has_data'])

    def test_elapsed_is_bucketed_by_minutes_not_seconds(self):
        # 差 90 秒：必须落「十分钟内」，不能单独成一档。
        self.make_verification(elapsed_seconds=90)
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['rows'][0]['count'], 1)
        self.assertEqual(report['stats']['median_label'], '十分钟内')

    def test_bucket_boundaries_are_left_closed(self):
        # 正好 600 秒：左闭右开，落「十分钟到一小时」不落「十分钟内」。
        self.make_verification(elapsed_seconds=600)
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['rows'][0]['count'], 0)
        self.assertEqual(report['stats']['rows'][1]['count'], 1)

    def test_window_is_taken_by_verified_at(self):
        # 40 天前发邮件、昨天才认证：窗口按 verified_at 取，这条该进周期。
        self._seq += 1
        user = make_user('old-token')
        row = CampusVerification.objects.create(
            user=user, campus_email=f'old@{self.domain}', domain_name='示例大学',
            status='verified', verified_at=timezone.now() - timedelta(days=1),
            token_issued_at=timezone.now() - timedelta(days=40),
        )
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)
        # 等了 40 天，落在最后一档。
        self.assertEqual(report['stats']['rows'][-1]['count'], 1)
        self.assertEqual(row.user.username, 'old-token')

    def test_records_outside_the_window_are_excluded(self):
        self.make_verification(elapsed_seconds=600, verified_days_ago=40)
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['measured_count'], 0)

    def test_domains_are_grouped_and_slow_ones_come_first(self):
        self.make_verification(
            elapsed_seconds=100000, domain_name='慢学校',
        )
        self.make_verification(elapsed_seconds=300, domain_name='慢学校')
        self.make_verification(elapsed_seconds=300, domain_name='快学校')
        self.make_verification(elapsed_seconds=300, domain_name='快学校')

        report = build_campus_verification_friction(days=30)
        names = [row['domain_name'] for row in report['domain_rows']]
        # 慢学校的「超过一天」比例高，排在前；但它只有 2 条样本，
        # 不能被样本量更大的快学校压下去。
        self.assertEqual(names[0], '慢学校')
        self.assertEqual(report['domain_rows'][0]['slow_rate'], 50.0)
        self.assertEqual(report['domain_rows'][1]['slow_rate'], 0.0)

    def test_domain_list_respects_the_limit(self):
        for index in range(DOMAIN_LIST_LIMIT + 3):
            self.make_verification(
                elapsed_seconds=300, domain_name=f'学校{index}',
            )
        report = build_campus_verification_friction(days=30)
        self.assertEqual(len(report['domain_rows']), DOMAIN_LIST_LIMIT)
        self.assertEqual(report['domain_row_total'], DOMAIN_LIST_LIMIT + 3)

    def test_slow_rows_are_sorted_by_elapsed_descending(self):
        self.make_verification(elapsed_seconds=90000)
        self.make_verification(elapsed_seconds=200000)
        # 没超过一天的不进这份清单。
        self.make_verification(elapsed_seconds=3600)

        report = build_campus_verification_friction(days=30)
        waits = [row['elapsed_seconds'] for row in report['slow_rows']]
        self.assertEqual(waits, sorted(waits, reverse=True))
        self.assertEqual(len(report['slow_rows']), 2)
        self.assertEqual(report['slow_row_total'], 2)

    def test_slow_rows_respect_the_list_limit(self):
        for index in range(SLOW_LIST_LIMIT + 3):
            self.make_verification(elapsed_seconds=90000 + index)
        report = build_campus_verification_friction(days=30)
        self.assertEqual(len(report['slow_rows']), SLOW_LIST_LIMIT)
        self.assertEqual(report['slow_row_total'], SLOW_LIST_LIMIT + 3)

    def test_small_sample_is_flagged(self):
        for _index in range(MIN_MEASURED_VERIFICATIONS - 1):
            self.make_verification(elapsed_seconds=600)
        report = build_campus_verification_friction(days=30)
        self.assertTrue(report['stats']['is_small_sample'])
        self.assertIn('样本不足', report['summary'])

    def test_negative_elapsed_is_clamped_to_zero(self):
        # 认证早于发邮件：数据里的一处矛盾（比如手工改过时间戳），
        # 不该让耗时变成负数，更不能让它从「十分钟内」里消失。
        self._seq += 1
        user = make_user('backwards')
        CampusVerification.objects.create(
            user=user, campus_email=f'backwards@{self.domain}',
            domain_name='示例大学', status='verified',
            verified_at=timezone.now() - timedelta(days=2),
            token_issued_at=timezone.now() - timedelta(days=1),
        )
        report = build_campus_verification_friction(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)
        self.assertEqual(report['stats']['median_label'], '十分钟内')

    def test_empty_report_is_still_well_formed(self):
        report = build_campus_verification_friction(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['stats']['measured_count'], 0)
        self.assertEqual(report['domain_rows'], [])
        self.assertEqual(report['slow_rows'], [])
        self.assertEqual(report['stats']['median_label'], '—')
        self.assertIn('0 次', report['summary'])

    def test_bucket_labels_match_the_declared_buckets(self):
        # 分档是模块对外的口径，改一处不改另一处会悄悄改掉报告的读法。
        report = build_campus_verification_friction(days=30)
        labels = [row['label'] for row in report['stats']['rows']]
        self.assertEqual(labels, [label for _limit, label, _hint in ELAPSED_BUCKETS])
