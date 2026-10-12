# -*- coding: utf-8 -*-
"""为「交易评价打了多少分」钉住口径。

`Rating` 的 `score` 一直在写，读路径只有商品详情页的 `Avg` / `Count` 摘要和后台列表。
这个模块把它读出来，只报告不改写。这些测试要钉住的是：

1. 覆盖率的分母只收「已经走完的交易」，`pending` 等未完结订单是右删失样本，单独数出来。
2. 「多久才评」按天分档；订单完成时刻取 `OrderEvent` 里 `to_status` 为 closed 的那一条，
   没有完成事件的评价不计入时延分布，单独数出来。
3. 评价人数分布按订单聚合，分母是订单数，一笔双方都评了的交易只出现一次。
4. 双向评价按方向拆开，不能只看总数——一个人评了就算「有评价」会骗人。
5. 和交付异常只做「有没有」对照，不判断因果；差值不满噪声门槛不给方向。
6. 分数分布只报六个桶，不合成任何「好评率」。
7. 自由文本只报「写了没写」和长度，一个字符的内容都不读。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    Category, Item, MeetingAppointment, MeetingIncident, Order, OrderEvent, Rating,
)
from .rating_quality import (
    COMMENT_LENGTH_BUCKETS, LATENCY_BUCKETS, MIN_MEASURED_ORDERS, NOISE_POINTS,
    RATING_COUNT_BUCKETS, SCORE_BUCKETS, UNRATED_LIST_LIMIT, build_rating_quality,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class RatingQualityTests(TestCase):
    def setUp(self):
        self.buyer = make_user('rating-buyer')
        self.seller = make_user('rating-seller')
        self.category = Category.objects.create(name='评价质量测试分类')
        self._seq = 0

    def make_item(self, *, trade_mode='sale', title='评价质量测试商品'):
        self._seq += 1
        return Item.objects.create(
            title=f'{title} {self._seq}', description='用于评价质量统计',
            price='10.00', category=self.category, condition='良好',
            seller=self.seller, trade_mode=trade_mode,
        )

    def make_closed_order(
        self, *, status='completed', trade_mode='sale', days_ago=2,
        buyer=None, item=None,
    ):
        """建一笔已经走完的交易，并补上它自己的完成事件。

        `Order.created_at` 是 auto_now_add，不能直接在 create 里给，只能建完再改。
        完成事件单独建，`created_at` 同样是 auto_now_add，也要建完再改。
        """
        item = item or self.make_item(trade_mode=trade_mode)
        order = Order.objects.create(
            item=item, buyer=buyer or self.buyer, seller=item.seller,
            meeting_location=None, agreed_price=item.price, status=status,
        )
        order.created_at = timezone.now() - timedelta(days=days_ago)
        order.save(update_fields=['created_at'])
        # 完成时刻比下单晚一天：这是评价资格的起点，也是时延分布的原点。
        completed_at = order.created_at + timedelta(days=1)
        event = OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='meeting',
            to_status=status, note='双方确认交易已完成',
        )
        event.created_at = completed_at
        event.save(update_fields=['created_at'])
        order.completed_at = completed_at
        return order

    def make_rating(self, order, *, rater=None, score=5, comment='', delay_days=1):
        """给一笔交易建一条评价，提交间隔按天给。"""
        rating = Rating.objects.create(
            order=order, rater=rater or order.buyer, ratee=order.seller,
            score=score, comment=comment,
        )
        rating.created_at = order.completed_at + timedelta(days=delay_days)
        rating.save(update_fields=['created_at'])
        return rating

    def make_incident(self, order):
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=order.seller, location=None,
            start_at=order.created_at + timedelta(days=1),
            end_at=order.created_at + timedelta(days=1, hours=1),
            status='confirmed', responded_by=order.buyer,
            responded_at=order.created_at + timedelta(hours=2),
        )
        incident = MeetingIncident.objects.create(
            appointment=appointment, reported_by=order.buyer, accused=order.seller,
            reason='no_show', detail='对方未到场', status='resolved',
        )
        return incident

    # --- 覆盖率的分母 -------------------------------------------------------

    def test_only_closed_orders_are_measured(self):
        self.make_closed_order(status='completed')
        self.make_closed_order(status='returned', trade_mode='borrow')
        # 还没走完的交易谈不到有没有被评价，是右删失样本。
        Order.objects.create(
            item=self.make_item(), buyer=self.buyer, seller=self.seller,
            meeting_location=None, agreed_price='10.00', status='pending',
        )

        report = build_rating_quality(days=30)
        self.assertEqual(report['coverage']['total'], 2)

    def test_open_orders_are_counted_separately_not_mixed_in(self):
        # 右删失样本单独数出来，不丢也不混：把它算进分母会让覆盖率凭空变小。
        self.make_closed_order()
        Order.objects.create(
            item=self.make_item(), buyer=self.buyer, seller=self.seller,
            meeting_location=None, agreed_price='10.00', status='meeting',
        )

        report = build_rating_quality(days=30)
        self.assertEqual(report['coverage']['total'], 1)
        self.assertEqual(report['coverage']['rating_rate'], 0.0)

    def test_both_sides_rated_is_the_only_healthy_shape(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)
        # 卖家也评买家：rater 是卖家，ratee 是买家。
        seller_rating = Rating.objects.create(
            order=order, rater=order.seller, ratee=order.buyer, score=4,
        )
        seller_rating.created_at = order.completed_at + timedelta(days=1)
        seller_rating.save(update_fields=['created_at'])

        report = build_rating_quality(days=30)
        self.assertEqual(report['coverage']['both_rated_count'], 1)
        self.assertEqual(report['coverage']['one_sided_count'], 0)
        self.assertEqual(report['coverage']['neither_rated_count'], 0)

    def test_one_sided_rating_is_not_reported_as_healthy(self):
        # 只报总数会骗人：只要有一个人评了就算「有评价」，另一个人可能永远不评。
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)

        report = build_rating_quality(days=30)
        self.assertEqual(report['coverage']['rated_count'], 1)
        self.assertEqual(report['coverage']['buyer_rated_count'], 0)
        self.assertEqual(report['coverage']['seller_rated_count'], 1)
        self.assertEqual(report['coverage']['one_sided_count'], 1)
        self.assertEqual(report['coverage']['both_rated_count'], 0)

    def test_rating_count_buckets_aggregate_by_order(self):
        # 一笔双方都评了的交易在分布里只出现一次：分母是订单数，不是评价数。
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)
        seller_rating = Rating.objects.create(
            order=order, rater=order.seller, ratee=order.buyer, score=5,
        )
        seller_rating.created_at = order.completed_at + timedelta(days=1)
        seller_rating.save(update_fields=['created_at'])

        report = build_rating_quality(days=30)
        rows = {row['label']: row['count'] for row in report['coverage']['rating_count_rows']}
        self.assertEqual(rows['双方都评价'], 1)
        self.assertEqual(rows['没人评价'], 0)

    # --- 时延分布 -----------------------------------------------------------

    def test_latency_uses_the_completion_event_not_the_order_timestamp(self):
        order = self.make_closed_order(days_ago=10)
        # 下单 10 天前，完成事件在下单后一天，评价在完成后 12 小时——真实等待是
        # 12 小时。若拿 order.created_at 当原点，这个样本会被算成 11 天。
        self.make_rating(order, delay_days=0.5)

        report = build_rating_quality(days=30)
        self.assertEqual(report['latency']['measured_count'], 1)
        self.assertEqual(
            report['latency']['bucket_rows'][0]['label'], '1 天内',
        )
        self.assertEqual(report['latency']['bucket_rows'][0]['count'], 1)

    def test_ratings_without_a_completion_event_are_counted_not_measured(self):
        order = self.make_closed_order()
        order.events.all().delete()
        self.make_rating(order, delay_days=3)

        report = build_rating_quality(days=30)
        self.assertEqual(report['latency']['measured_count'], 0)
        self.assertEqual(report['latency']['missing_event_count'], 1)

    def test_latency_is_bucketed_by_days_not_hours(self):
        order = self.make_closed_order()
        self.make_rating(order, delay_days=5)

        report = build_rating_quality(days=30)
        rows = {row['label']: row['count'] for row in report['latency']['bucket_rows']}
        self.assertEqual(rows['3-7 天'], 1)
        self.assertEqual(rows['1 天内'], 0)

    def test_late_ratings_are_flagged_separately(self):
        order = self.make_closed_order()
        self.make_rating(order, delay_days=45)

        report = build_rating_quality(days=30)
        self.assertEqual(report['latency']['late_count'], 1)
        self.assertEqual(report['latency']['late_rate'], 100.0)

    def test_unrated_orders_only_report_scale_not_waiting_time(self):
        # 还没评的订单是右删失样本，而且它跨周期：只报规模，不报「已经等了多久」。
        self.make_closed_order()

        report = build_rating_quality(days=30)
        self.assertEqual(report['latency']['unrated_count'], 1)
        self.assertEqual(report['latency']['measured_count'], 0)

    # --- 分数分布 -----------------------------------------------------------

    def test_score_distribution_reports_all_buckets_without_a_good_rate(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)
        for score in (1, 2, 3, 4):
            other = self.make_closed_order()
            self.make_rating(other, rater=other.buyer, score=score)

        report = build_rating_quality(days=30)
        rows = {row['label']: row['count'] for row in report['score']['bucket_rows']}
        self.assertEqual(rows, {'1 星': 1, '2 星': 1, '3 星': 1, '4 星': 1, '5 星': 1})
        # 不合成任何「好评率」。
        self.assertNotIn('good_rate', report['score'])
        self.assertNotIn('satisfaction', report['score'])

    def test_low_score_is_one_star_and_two_star_only(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=2)

        report = build_rating_quality(days=30)
        self.assertEqual(report['score']['low_count'], 1)
        self.assertEqual(report['score']['low_rate'], 100.0)

    def test_average_is_reported_but_not_reused_as_a_badge(self):
        # Avg 已经在 reputation.py 里了，本模块只报它，不新增任何徽章。
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=4)

        report = build_rating_quality(days=30)
        self.assertEqual(report['score']['average'], 4.0)
        self.assertNotIn('badges', report)

    # --- 交付异常对照 -------------------------------------------------------

    def test_incident_contrast_only_reports_no_causation(self):
        # 带异常的和不带的各自打分；报告里不出现任何「因为」。
        clean = self.make_closed_order()
        self.make_rating(clean, rater=clean.buyer, score=5)
        flagged = self.make_closed_order()
        self.make_rating(flagged, rater=flagged.buyer, score=1)
        self.make_incident(flagged)

        report = build_rating_quality(days=30)
        contrast = report['incident_contrast']
        self.assertTrue(contrast['has_both_sides'])
        rows = {row['key']: row for row in contrast['rows']}
        self.assertEqual(rows['incident']['low_rate'], 100.0)
        self.assertEqual(rows['clean']['low_rate'], 0.0)
        self.assertNotIn('because', report['summary'])
        self.assertNotIn('导致', report['summary'])

    def test_incident_contrast_is_noise_when_the_gap_is_small(self):
        # 差值不满 8 个百分点视为噪声，不给方向。
        for _ in range(6):
            clean = self.make_closed_order()
            self.make_rating(clean, rater=clean.buyer, score=5)
        flagged = self.make_closed_order()
        self.make_rating(flagged, rater=flagged.buyer, score=5)
        self.make_incident(flagged)

        report = build_rating_quality(days=30)
        self.assertTrue(report['incident_contrast']['is_noise'])
        self.assertEqual(report['incident_contrast']['noise_points'], NOISE_POINTS)

    def test_only_confirmed_incidents_count(self):
        # 还没处理完的异常不进对照：它只是一条申诉，不是事实。
        flagged = self.make_closed_order()
        self.make_rating(flagged, rater=flagged.buyer, score=1)
        appointment = MeetingAppointment.objects.create(
            order=flagged, proposed_by=flagged.seller, location=None,
            start_at=flagged.created_at + timedelta(days=1),
            end_at=flagged.created_at + timedelta(days=1, hours=1),
            status='confirmed', responded_by=flagged.buyer,
            responded_at=flagged.created_at + timedelta(hours=2),
        )
        MeetingIncident.objects.create(
            appointment=appointment, reported_by=flagged.buyer,
            accused=flagged.seller, reason='safety', detail='待核实', status='open',
        )

        report = build_rating_quality(days=30)
        rows = {row['key']: row for row in report['incident_contrast']['rows']}
        self.assertEqual(rows['incident']['order_count'], 0)

    # --- 自由文本 -----------------------------------------------------------

    def test_comment_only_reports_fill_rate_and_length(self):
        order = self.make_closed_order()
        self.make_rating(
            order, rater=order.buyer, score=5, comment='交易很顺利，卖家很好沟通',
        )
        other = self.make_closed_order()
        self.make_rating(other, rater=other.buyer, score=1, comment='')

        report = build_rating_quality(days=30)
        comment = report['comment']
        self.assertEqual(comment['rating_count'], 2)
        self.assertEqual(comment['filled_count'], 1)
        self.assertEqual(comment['fill_rate'], 50.0)
        # 一个字符的内容都不读：报告里没有原文。
        self.assertNotIn('交易很顺利', str(report))

    def test_comment_length_buckets_are_fixed(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5, comment='短评')
        other = self.make_closed_order()
        self.make_rating(other, rater=other.buyer, score=5, comment='长' * 200)

        report = build_rating_quality(days=30)
        rows = {row['label']: row['count'] for row in report['comment']['length_rows']}
        self.assertEqual(rows['20 字以内'], 1)
        self.assertEqual(rows['150 字以上'], 1)
        self.assertEqual(len(report['comment']['length_rows']), len(COMMENT_LENGTH_BUCKETS))

    def test_wrote_vs_silent_contrast_is_reported(self):
        # 一星是不是更容易配一段话——这是 comment 唯一有信息量的一问。
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=1, comment='东西和说的不一样')
        other = self.make_closed_order()
        self.make_rating(other, rater=other.buyer, score=5, comment='')

        report = build_rating_quality(days=30)
        comment = report['comment']
        self.assertEqual(comment['wrote_average'], 1.0)
        self.assertEqual(comment['silent_average'], 5.0)
        self.assertEqual(comment['average_gap'], -4.0)

    # --- 交易方式 -----------------------------------------------------------

    def test_trade_modes_are_split_not_merged(self):
        self.make_closed_order(trade_mode='sale')
        self.make_closed_order(trade_mode='free')
        self.make_closed_order(trade_mode='borrow', status='returned')

        report = build_rating_quality(days=30)
        labels = {row['key'] for row in report['trade_mode']['rows']}
        self.assertEqual(labels, {'sale', 'free', 'borrow'})

    # --- 样本门槛与边界 -----------------------------------------------------

    def test_small_sample_is_flagged_not_hidden(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)

        report = build_rating_quality(days=30)
        self.assertTrue(report['score']['is_small_sample'])
        self.assertEqual(report['score']['min_measured_orders'], MIN_MEASURED_ORDERS)

    def test_empty_period_reports_no_data_without_crashing(self):
        report = build_rating_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['coverage']['total'], 0)
        self.assertEqual(report['score']['total'], 0)
        self.assertIsNone(report['score']['average'])

    def test_unknown_days_falls_back_to_thirty(self):
        report = build_rating_quality(days=13)
        self.assertEqual(report['period_days'], 30)

    def test_unrated_list_is_capped(self):
        for _ in range(UNRATED_LIST_LIMIT + 3):
            self.make_closed_order()

        report = build_rating_quality(days=30)
        self.assertEqual(report['latency']['unrated_row_total'], UNRATED_LIST_LIMIT + 3)
        self.assertEqual(len(report['latency']['unrated_rows']), UNRATED_LIST_LIMIT)

    def test_summary_mentions_the_sample_floor_when_small(self):
        order = self.make_closed_order()
        self.make_rating(order, rater=order.buyer, score=5)

        report = build_rating_quality(days=30)
        self.assertIn('占比只作参考', report['summary'])