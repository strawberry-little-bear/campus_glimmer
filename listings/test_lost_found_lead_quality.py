# -*- coding: utf-8 -*-
"""为「失物招领的线索，有多少条走到了已确认」钉住口径。

这条链路每一步都在写库，却从来没有人把它们连起来看过。这个模块把它们读出来，
只报告不改写。这些测试要钉住的是：

1. 确认率按 `post_type` 拆开报，不按总数报——lost 和 found 是两个完全不同的处境。
2. 「一条记录收到几条线索」的分母是记录数，不是线索数。
3. 被批量清掉的线索单独数，不算「发布者拒绝」——这是本模块存在的首要理由。
4. 线索提交间隔按小时分档，只报第一条。
5. 完成率只报失物招领自己的，不和求购响应合并。
6. 已确认但没提交反馈的单独数，不猜测是哪一种。
7. 线索说明和辨识特征只报长度，一个字符的内容都不读。
8. 不按用户聚合、不排名。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .lost_found_lead_quality import (
    LEAD_LATENCY_BUCKETS, MESSAGE_LENGTH_BUCKETS, MIN_MEASURED_POSTS,
    build_lost_found_lead_quality,
)
from .models import Category, CampusLocation, DemandPost, DemandResponse, Item, LostFoundLead, LostFoundPost, MutualAidFeedback


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class LostFoundLeadQualityTests(TestCase):
    def setUp(self):
        self.reporter = make_user('lostfound-reporter')
        self.respondent = make_user('lostfound-respondent')
        self.category = Category.objects.create(name='失物线索测试分类')
        self.location = CampusLocation.objects.create(name='失物线索测试地点')
        self._seq = 0

    def make_post(self, *, post_type='lost', created_days_ago=3, status='active',
                  features=''):
        """建一条失物招领记录。`created_at` 是 auto_now_add，建完再改。"""
        self._seq += 1
        post = LostFoundPost.objects.create(
            reporter=self.reporter, post_type=post_type,
            title=f'失物线索测试记录 {self._seq}',
            description='用于失物招领线索质量统计', category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(days=created_days_ago + 1),
            identifying_features=features,
        )
        post.created_at = timezone.now() - timedelta(days=created_days_ago)
        post.save(update_fields=['created_at'])
        if status != 'active':
            post.status = status
            post.save(update_fields=['status'])
        return post

    def make_lead(self, post, *, respondent=None, status='pending', delay_hours=2,
                  message='我在食堂三楼捡到一个黑色书包。'):
        """给一条记录提一条线索。`created_at` 是 auto_now_add，建完再改。"""
        lead = LostFoundLead.objects.create(
            post=post, respondent=respondent or self.respondent,
            message=message, status=status,
        )
        lead.created_at = post.created_at + timedelta(hours=delay_hours)
        lead.save(update_fields=['created_at'])
        return lead

    def make_feedback(self, lead, *, outcome='completed'):
        """给一条已确认的线索提交反馈。"""
        return MutualAidFeedback.objects.create(
            lost_found_lead=lead, submitted_by=lead.post.reporter,
            outcome=outcome, tags=[], note='',
        )

    # --- 线索分布 -------------------------------------------------------

    def test_lead_count_is_counted_per_post_not_per_lead(self):
        # 分母是记录数。一条收到三条线索的记录在分布里只出现一次。
        post = self.make_post()
        for index in range(3):
            self.make_lead(post, respondent=make_user(f'lf-respondent-{index}'))
        other = self.make_post()
        self.make_lead(other, respondent=make_user('lf-single'))

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['lead_count']['total'], 2)
        self.assertEqual(report['lead_count']['lead_count'], 4)
        self.assertEqual(report['lead_count']['leads_per_post'], 2.0)
        # 「两三条线索」那一档是 1 条记录，不是 3 条。
        row = report['lead_count']['lead_count_rows'][2]
        self.assertEqual(row['label'], '两三条线索')
        self.assertEqual(row['count'], 1)
        # 一条线索的那条记录落在「只有一条线索」，不落进「两三条」。
        self.assertEqual(report['lead_count']['lead_count_rows'][1]['count'], 1)
        self.assertEqual(report['lead_count']['lead_count_rows'][3]['count'], 0)

    def test_posts_without_any_lead_are_counted(self):
        # 发布了但没人提线索，和发布了收到一条线索，在总数上长得一模一样。
        self.make_post()
        self.make_post()
        post = self.make_post()
        self.make_lead(post)

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['lead_count']['posts_without_lead_count'], 2)
        self.assertEqual(report['lead_count']['posts_without_lead_rate'], 66.7)
        self.assertEqual(report['lead_count']['posts_with_lead_count'], 1)

    # --- 确认率按类型拆 ---------------------------------------------------

    def test_confirm_rate_is_split_by_post_type(self):
        # lost 和 found 是两个完全不同的处境，合并成一个数无法定位问题。
        for _ in range(3):
            post = self.make_post(post_type='lost')
            self.make_lead(post, status='accepted')
        for _ in range(3):
            post = self.make_post(post_type='found')
            self.make_lead(post, status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rows = {row['key']: row for row in report['confirm']['rows']}
        self.assertEqual(rows['lost']['confirm_rate'], 100.0)
        self.assertEqual(rows['found']['confirm_rate'], 0.0)
        self.assertEqual(report['confirm']['confirm_rate'], 50.0)

    def test_confirm_rate_denominator_is_post_count(self):
        # 一条记录只会有一条 accepted（确认时其余 pending 被批量清掉），
        # 所以分子天然不超过 1，分母必须是记录数。
        post = self.make_post()
        for index in range(3):
            self.make_lead(post, respondent=make_user(f'lf-denom-{index}'))
        self.make_lead(post, status='accepted', respondent=make_user('lf-denom-acc'))

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['confirm']['post_count'], 1)
        self.assertEqual(report['confirm']['accepted_count'], 1)
        self.assertEqual(report['confirm']['confirm_rate'], 100.0)

    # --- 批量清理 vs 发布者拒绝 -------------------------------------------

    def test_bulk_cleared_leads_are_not_counted_as_reporter_rejection(self):
        # 这是本模块存在的首要理由。一条 accept 会把同一条记录下其余 pending
        # 全部改成 rejected，这些不是发布者拒绝的。
        post = self.make_post()
        accepted = self.make_lead(post, respondent=make_user('lf-bulk-a'), status='accepted')
        for index in range(3):
            self.make_lead(post, respondent=make_user(f'lf-bulk-{index}'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rejection = report['rejection']
        self.assertEqual(rejection['rejected_count'], 3)
        self.assertEqual(rejection['bulk_cleared_count'], 3)
        self.assertEqual(rejection['bulk_cleared_share_of_rejected'], 100.0)
        # 发布者一条都没拒绝过。
        self.assertEqual(rejection['rejected_by_reporter_count'], 0)
        self.assertEqual(rejection['posts_with_rejection_count'], 0)
        self.assertEqual(rejection['posts_with_rejection_rate'], 0.0)

    def test_reporter_rejection_requires_no_accepted_lead_on_the_post(self):
        # 记录上没有 accepted 的线索时，rejected 才归「发布者看过之后拒绝」。
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-true-a'), status='rejected')
        self.make_lead(post, respondent=make_user('lf-true-b'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rejection = report['rejection']
        self.assertEqual(rejection['rejected_count'], 2)
        self.assertEqual(rejection['rejected_by_reporter_count'], 2)
        self.assertEqual(rejection['bulk_cleared_count'], 0)
        self.assertEqual(rejection['posts_with_rejection_count'], 1)
        self.assertEqual(rejection['posts_with_rejection_rate'], 100.0)

    def test_rejected_leads_on_a_post_with_accepted_are_all_bulk_cleared(self):
        # 一条记录上只要出现过 accepted，它其余的 rejected 就全部归「顺带清掉」。
        # 这不是猜测，是能拿到的最好判据：状态字段分不开这两种情况，而记录上
        # 有没有 accepted 是唯一可用的信号。这个测试把这条边界钉住——它偏保守，
        # 会把「发布者真的拒绝过、后来又确认了另一条」的那几条也算成顺带清掉，
        # 但反过来做（算成发布者拒绝）会系统性地高估发布者的拒绝行为，而高估
        # 比低估更危险：它会直接导致去提醒发布者多看看线索这种错误动作。
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-mix-a'), status='rejected')
        self.make_lead(post, respondent=make_user('lf-mix-b'), status='accepted')
        self.make_lead(post, respondent=make_user('lf-mix-c'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rejection = report['rejection']
        self.assertEqual(rejection['rejected_count'], 2)
        self.assertEqual(rejection['rejected_by_reporter_count'], 0)
        self.assertEqual(rejection['bulk_cleared_count'], 2)

    def test_reporter_rejection_is_only_reported_when_no_lead_was_accepted(self):
        # 记录上没有 accepted 时，rejected 才归「发布者看过之后拒绝」。这条边界
        # 和上一条合起来，把整个拆分规则的两侧都钉住。
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-split-a'), status='rejected')
        self.make_lead(post, respondent=make_user('lf-split-b'), status='rejected')
        other = self.make_post()
        self.make_lead(other, respondent=make_user('lf-split-c'), status='accepted')
        self.make_lead(other, respondent=make_user('lf-split-d'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rejection = report['rejection']
        # 两条真的被发布者拒绝，一条被顺带清掉。
        self.assertEqual(rejection['rejected_by_reporter_count'], 2)
        self.assertEqual(rejection['bulk_cleared_count'], 1)
        # 发布者拒绝率的分母是发起过线索的记录数。
        self.assertEqual(rejection['posts_with_leads_count'], 2)
        self.assertEqual(rejection['posts_with_rejection_count'], 1)
        self.assertEqual(rejection['posts_with_rejection_rate'], 50.0)

    def test_withdrawn_leads_are_reported_separately(self):
        # 撤回和拒绝对发布者的意义完全相反：前者说明有人本来想帮忙又改了主意。
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-wd-a'), status='withdrawn')
        self.make_lead(post, respondent=make_user('lf-wd-b'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        rejection = report['rejection']
        self.assertEqual(rejection['withdrawn_count'], 1)
        self.assertEqual(rejection['withdrawn_share'], 50.0)
        # 撤回不算进 rejected。
        self.assertEqual(rejection['rejected_count'], 1)

    def test_pending_and_accepted_leads_are_not_rejections(self):
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-pa-a'), status='pending')
        self.make_lead(post, respondent=make_user('lf-pa-b'), status='accepted')

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['rejection']['rejected_count'], 0)
        self.assertEqual(report['rejection']['bulk_cleared_count'], 0)
        self.assertEqual(report['rejection']['rejected_by_reporter_count'], 0)

    # --- 提交间隔 -------------------------------------------------------

    def test_first_lead_latency_uses_the_earliest_lead_only(self):
        # 只报第一条：它是「这条记录有没有被看见」的第一个信号，后续几条是热度。
        post = self.make_post(created_days_ago=5)
        self.make_lead(post, respondent=make_user('lf-lat-a'), delay_hours=48)
        self.make_lead(post, respondent=make_user('lf-lat-b'), delay_hours=1)

        report = build_lost_found_lead_quality(days=30)
        latency = report['latency']
        self.assertEqual(latency['measured_count'], 1)
        self.assertEqual(latency['median_label'], '1 小时')
        self.assertEqual(latency['same_day_count'], 1)
        self.assertEqual(latency['same_day_rate'], 100.0)

    def test_lead_latency_buckets_are_hourly_not_daily(self):
        # 失物招领的时间尺度是小时到天，分档必须按小时起步。
        self.assertEqual(LEAD_LATENCY_BUCKETS[0][0], 6 * 3600)
        self.assertEqual(LEAD_LATENCY_BUCKETS[1][0], 86400)
        self.assertEqual(LEAD_LATENCY_BUCKETS[-1][0], None)

    def test_lead_before_post_is_excluded_from_latency(self):
        # 线索早于记录：数据被改过，算不出真实等待时长，不塞进任何一档。
        post = self.make_post(created_days_ago=2)
        self.make_lead(post, respondent=make_user('lf-neg-a'), delay_hours=-48)

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['latency']['measured_count'], 0)
        self.assertEqual(report['latency']['median_label'], '—')

    # --- 完成反馈 -------------------------------------------------------

    def test_feedback_only_counts_lost_found_source(self):
        # 只报失物招领自己的，不和求购响应合并。
        post = self.make_post()
        lead = self.make_lead(post, status='accepted')
        self.make_feedback(lead, outcome='completed')

        buyer = make_user('lf-fb-buyer')
        seller = make_user('lf-fb-seller')
        demand = DemandPost.objects.create(
            requester=buyer, title='求购测试', description='用于区分来源',
            min_price='50.00',
        )
        item = Item.objects.create(
            title='求购响应测试商品', description='用于区分来源', price='50.00',
            category=self.category, condition='良好', seller=seller,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=seller,
        )
        MutualAidFeedback.objects.create(
            demand_response=response, submitted_by=buyer, outcome='unresolved',
            tags=[], note='',
        )

        report = build_lost_found_lead_quality(days=30)
        feedback = report['feedback']
        self.assertEqual(feedback['accepted_count'], 1)
        self.assertEqual(feedback['feedback_count'], 1)
        self.assertEqual(feedback['completed_count'], 1)
        self.assertEqual(feedback['completion_rate'], 100.0)

    def test_accepted_without_feedback_is_counted_without_guessing(self):
        # accept 分支里没有任何东西引导发布者之后去填反馈，这是一个真实存在的
        # 中间状态，单独数出来，不猜测是找回来了还是没找回来。
        post = self.make_post()
        self.make_lead(post, status='accepted')
        other = self.make_post()
        lead = self.make_lead(other, status='accepted')
        self.make_feedback(lead, outcome='unresolved')

        report = build_lost_found_lead_quality(days=30)
        feedback = report['feedback']
        self.assertEqual(feedback['accepted_count'], 2)
        self.assertEqual(feedback['feedback_count'], 1)
        self.assertEqual(feedback['without_feedback_count'], 1)
        self.assertTrue(feedback['without_feedback_note'])
        self.assertEqual(feedback['completion_rate'], 0.0)

    def test_feedback_completion_rate_ignores_records_without_feedback(self):
        # 完成率的分母是提交了反馈的那些，不是已确认的那些。
        post = self.make_post()
        self.make_lead(post, status='accepted')
        for index in range(2):
            other = self.make_post()
            lead = self.make_lead(other, status='accepted')
            self.make_feedback(lead, outcome='completed')

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['feedback']['accepted_count'], 3)
        self.assertEqual(report['feedback']['feedback_count'], 2)
        self.assertEqual(report['feedback']['completion_rate'], 100.0)

    # --- 自由文本 -------------------------------------------------------

    def test_message_length_is_reported_without_reading_content(self):
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-msg-a'), message='短')
        self.make_lead(post, respondent=make_user('lf-msg-b'), message='我在食堂三楼捡到一个黑色书包，侧面有一个蓝色挂件。')
        self.make_lead(post, respondent=make_user('lf-msg-c'), message='   ')

        report = build_lost_found_lead_quality(days=30)
        message = report['message']
        self.assertEqual(message['lead_count'], 3)
        self.assertEqual(message['filled_count'], 2)
        # 空白字符串不算「写了」。
        self.assertEqual(message['empty_count'], 1)
        self.assertEqual(message['average_length'], 13.0)
        self.assertEqual(MESSAGE_LENGTH_BUCKETS[0][1], '20 字以内')

    def test_identifying_features_fill_rate_is_reported(self):
        # 辨识特征是为了让双方核对的，只报填了没有，不读内容。
        self.make_post()
        self.make_post(features='黑色，侧面有挂件')

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['message']['feature_filled_count'], 1)
        self.assertEqual(report['message']['feature_fill_rate'], 50.0)

    # --- 边界与空数据 ----------------------------------------------------

    def test_empty_period_reports_no_data(self):
        report = build_lost_found_lead_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertIn('当前周期还没有失物招领记录', report['summary'])
        self.assertEqual(report['lead_count']['total'], 0)
        self.assertEqual(report['confirm']['confirm_rate'], 0.0)
        self.assertEqual(report['rejection']['lead_count'], 0)
        self.assertEqual(report['feedback']['accepted_count'], 0)

    def test_small_sample_is_flagged(self):
        # 不足 MIN_MEASURED_POSTS 条时明确标出来，占比只作参考。
        self.make_post()
        report = build_lost_found_lead_quality(days=30)
        self.assertTrue(report['lead_count']['is_small_sample'])
        self.assertEqual(report['lead_count']['min_measured_posts'], MIN_MEASURED_POSTS)
        self.assertIn(f'不足 {MIN_MEASURED_POSTS} 条', report['summary'])

    def test_records_outside_the_window_are_excluded(self):
        # 窗口外的记录不进来。
        old = self.make_post(created_days_ago=45)
        self.make_lead(old)
        fresh = self.make_post(created_days_ago=2)
        self.make_lead(fresh)

        report = build_lost_found_lead_quality(days=30)
        self.assertEqual(report['lead_count']['total'], 1)

    def test_invalid_days_falls_back_to_thirty(self):
        # 传进来的天数不在可选范围里时退回 30 天，不报错也不静默用错窗口。
        post = self.make_post()
        self.make_lead(post)
        report = build_lost_found_lead_quality(days=14)
        self.assertEqual(report['period_days'], 30)
        self.assertEqual(report['lead_count']['total'], 1)

    def test_summary_mentions_bulk_cleared_share(self):
        # 摘要要把「这些驳回不是发布者拒绝的」讲出来，这是本模块最要紧的一句。
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-sum-a'), status='accepted')
        self.make_lead(post, respondent=make_user('lf-sum-b'), status='rejected')

        report = build_lost_found_lead_quality(days=30)
        self.assertIn('顺带清掉', report['summary'])
        self.assertIn('不是发布者拒绝的', report['summary'])

    def test_pending_rows_are_listed_for_follow_up(self):
        post = self.make_post()
        self.make_lead(post, respondent=make_user('lf-pend-a'), status='pending')
        self.make_lead(post, respondent=make_user('lf-pend-b'), status='accepted')

        report = build_lost_found_lead_quality(days=30)
        pending = report['pending_rows']
        self.assertEqual(pending['row_total'], 1)
        self.assertEqual(len(pending['rows']), 1)
        self.assertEqual(pending['rows'][0]['lead'].status, 'pending')
