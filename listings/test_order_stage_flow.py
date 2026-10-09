# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import CampusLocation, Category, Item, Order, OrderEvent
from .order_stage_flow import (
    SLOW_STAGE_SECONDS,
    STAGES,
    STAGE_KEYS,
    _format_duration,
    _order_timeline,
    _percentile,
    build_order_stage_flow,
)

# The forward chain an order walks. Used to derive the from_status a real
# transition writes, so the events built here look like production data.
FORWARD_CHAIN = ('pending', 'confirmed', 'meeting', 'completed')


def _status_before(current_status, target_status):
    """Status an order leaves when it moves to the target status."""
    if target_status in FORWARD_CHAIN and current_status in FORWARD_CHAIN:
        if FORWARD_CHAIN.index(target_status) > FORWARD_CHAIN.index(current_status):
            return current_status
    return current_status


class OrderStageFlowTests(TestCase):
    """Cover the stage timeline, its censoring rules and the bottleneck pick."""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='stage-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='stage-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='阶段测试分类')
        self.location = CampusLocation.objects.create(name='阶段测试地点')
        self.now = timezone.now()
        self._current_status = {}

    def make_item(self, title='阶段测试商品', category=None, trade_mode='sale'):
        return Item.objects.create(
            title=title,
            description='用于验证订单阶段时长统计。',
            category=category or self.category,
            location=self.location,
            seller=self.seller,
            price=10,
            trade_mode=trade_mode,
            status='reserved',
        )

    def make_order(self, *, title='阶段测试商品', created_ago=timedelta(0),
                   status='completed', location='default', category=None, trade_mode='sale'):
        """Create an order with a pinned created_at.

        created_at is auto_now_add, so it has to be rewritten through update()
        to land inside the measured period; otherwise every row would sit just
        after the reference instant and be filtered out.
        """
        order = Order.objects.create(
            item=self.make_item(title, category, trade_mode),
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location if location == 'default' else location,
            agreed_price=10,
            status=status,
        )
        created_at = self.now - created_ago
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
        return order, created_at

    def add_event(self, order, *, to_status, after, note='', from_status=None, current=None):
        """Write one event at a fixed offset from the order creation.

        current is the status the order is in when the event fires. It defaults
        to the last status this helper wrote, so a chain of calls reproduces the
        from_status/to_status pairs the production transition paths write.
        Passing from_status explicitly builds the action-only events that keep
        the current status.
        """
        if current is None:
            current = self._current_status.get(order.pk, order.status)
        if from_status is None:
            from_status = _status_before(current, to_status)
        event = OrderEvent.objects.create(
            order=order,
            actor=self.seller,
            from_status=from_status,
            to_status=to_status,
            note=note,
        )
        OrderEvent.objects.filter(pk=event.pk).update(created_at=after)
        event.refresh_from_db()
        if from_status != to_status:
            self._current_status[order.pk] = to_status
        return event

    def build_full_chain(self, *, confirm=timedelta(hours=2), meeting=timedelta(days=1),
                         complete=timedelta(days=2), created_ago=timedelta(0),
                         location='default', category=None, title='阶段测试商品',
                         trade_mode='sale'):
        """One order that walks every stage, with per-stage offsets."""
        order, created_at = self.make_order(
            title=title, created_ago=created_ago, location=location,
            category=category, trade_mode=trade_mode,
        )
        # A lend closes at "borrowed" instead of "completed".
        closed_status = 'borrowed' if trade_mode == 'borrow' else 'completed'
        self.add_event(order, to_status='confirmed', after=created_at + confirm)
        self.add_event(order, to_status='meeting', after=created_at + confirm + meeting)
        self.add_event(
            order, to_status=closed_status,
            after=created_at + confirm + meeting + complete,
        )
        return order, created_at

    def events_of(self, order):
        return list(
            OrderEvent.objects.filter(order=order)
            .values('to_status', 'from_status', 'created_at')
            .order_by('created_at', 'id')
        )

    # --- helpers -------------------------------------------------------

    def test_percentile_uses_nearest_rank(self):
        self.assertIsNone(_percentile([], 0.5))
        self.assertEqual(_percentile([10], 0.5), 10)
        self.assertEqual(_percentile([1, 2, 3, 4], 0.5), 3)
        self.assertEqual(_percentile([1, 2, 3, 4], 0.9), 4)
        # An even count rounds to the nearer rank rather than averaging.
        self.assertEqual(_percentile([1, 2, 3], 0.5), 2)

    def test_format_duration_picks_the_coarsest_readable_unit(self):
        self.assertEqual(_format_duration(None), '—')
        self.assertEqual(_format_duration(30), '30 秒')
        self.assertEqual(_format_duration(600), '10 分钟')
        self.assertEqual(_format_duration(7200), '2 小时')
        self.assertEqual(_format_duration(3 * 86400), '3 天')
        self.assertEqual(_format_duration(-5), '0 秒')

    def test_stage_chain_is_aligned_with_milestones(self):
        # A stage can end at more than one status: closing a deal is recorded as
        # "completed" for a sale and "borrowed" for a lend.
        self.assertEqual(STAGE_KEYS[0], 'placed_to_confirmed')
        self.assertEqual(
            [stage['milestones'] for stage in STAGES],
            [('confirmed',), ('meeting',), ('completed', 'borrowed'), ('returned',)],
        )

    def test_timeline_ignores_events_that_keep_the_current_status(self):
        """Check-in, scheduling and dispute events are actions, not milestones."""
        order, created_at = self.make_order(status='meeting')
        self.add_event(
            order, to_status='meeting', after=created_at + timedelta(hours=1),
            note='双方均已登记到场', from_status='meeting',
        )
        self.add_event(
            order, to_status='meeting', after=created_at + timedelta(hours=2),
            note='交易争议已提交，等待平台处理', from_status='meeting',
        )
        timeline, dropped = _order_timeline(created_at, self.events_of(order))

        # No stage was actually completed, so the timeline stays empty.
        self.assertEqual(timeline, {})
        self.assertEqual(dropped, 0)

    def test_timeline_drops_a_stage_recorded_before_its_predecessor(self):
        """Backfilled or clock-skewed events must not create a negative stage."""
        order, created_at = self.make_order()
        self.add_event(order, to_status='meeting', after=created_at + timedelta(hours=1))
        self.add_event(
            order, to_status='confirmed', after=created_at - timedelta(hours=2),
        )
        timeline, dropped = _order_timeline(created_at, self.events_of(order))

        # Both events arrive in chain order "meeting" then "confirmed", so the
        # meeting event is accepted first and the confirmed one is dropped: an
        # order cannot be confirmed after it has already been handed over.
        self.assertEqual(
            timeline.get('confirmed_to_meeting'), created_at + timedelta(hours=1),
        )
        self.assertNotIn('placed_to_confirmed', timeline)
        self.assertEqual(dropped, 1)

    # --- stage measurement --------------------------------------------

    def test_every_stage_of_a_completed_order_is_measured(self):
        self.build_full_chain()
        result = build_order_stage_flow(days=30, now=self.now)

        self.assertTrue(result['has_data'])
        rows = {row['key']: row for row in result['stage_rows']}
        self.assertEqual(rows['placed_to_confirmed']['sample_size'], 1)
        self.assertEqual(rows['placed_to_confirmed']['median_label'], '2 小时')
        self.assertEqual(rows['confirmed_to_meeting']['median_label'], '1 天')
        self.assertEqual(rows['meeting_to_closed']['median_label'], '2 天')
        # The return stage only exists for borrowed goods.
        self.assertEqual(rows['borrowed_to_returned']['sample_size'], 0)

        summary = result['summary']
        self.assertEqual(summary['total_orders'], 1)
        self.assertEqual(summary['measured_orders'], 1)
        self.assertEqual(summary['measured_share'], 100.0)
        self.assertEqual(summary['end_to_end_median_label'], '3 天')
        self.assertEqual(summary['dropped_samples'], 0)
        self.assertEqual(summary['censored_orders'], 0)

    def test_an_order_still_waiting_is_censored_not_slow(self):
        """A pending order is inside a stage, not finished with it."""
        self.make_order(status='pending', created_ago=timedelta(days=5))
        result = build_order_stage_flow(days=30, now=self.now)

        summary = result['summary']
        self.assertEqual(summary['total_orders'], 1)
        # No stage reached both ends, so nothing is measured.
        self.assertEqual(summary['measured_orders'], 0)
        self.assertEqual(summary['measured_share'], 0)
        # It is reported as still inside the first stage instead.
        self.assertEqual(summary['censored_orders'], 1)
        self.assertEqual(summary['unconfirmed_orders'], 1)
        rows = {row['key']: row for row in result['stage_rows']}
        self.assertEqual(rows['placed_to_confirmed']['still_in_stage'], 1)
        self.assertEqual(rows['placed_to_confirmed']['sample_size'], 0)
        self.assertIsNone(result['bottleneck'])

    def test_a_stale_order_from_before_the_period_is_excluded(self):
        """Only orders created inside the period are measured."""
        self.build_full_chain(created_ago=timedelta(days=400))
        result = build_order_stage_flow(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['total_orders'], 0)

    def test_bottleneck_names_the_stage_owning_the_most_median_time(self):
        # Fast confirmation, slow handoff: the second stage should win.
        # Three orders are needed to clear the sample threshold.
        for index in range(3):
            self.build_full_chain(
                title=f'阶段测试商品 {index}',
                confirm=timedelta(minutes=5 + index),
                meeting=timedelta(days=9 - index),
            )
        result = build_order_stage_flow(days=30, now=self.now)

        bottleneck = result['bottleneck']
        self.assertIsNotNone(bottleneck)
        self.assertEqual(bottleneck['key'], 'confirmed_to_meeting')
        self.assertEqual(bottleneck['sample_size'], 3)
        self.assertGreater(bottleneck['share_of_measured_total'], 50)

        rows = {row['key']: row for row in result['stage_rows']}
        self.assertTrue(rows['confirmed_to_meeting']['is_bottleneck'])
        self.assertFalse(rows['placed_to_confirmed']['is_bottleneck'])

    def test_a_stage_below_the_sample_threshold_is_not_flagged(self):
        """A single outlier must not become a recommendation."""
        self.build_full_chain(meeting=timedelta(days=30))
        result = build_order_stage_flow(days=30, now=self.now)

        rows = {row['key']: row for row in result['stage_rows']}
        self.assertGreater(rows['confirmed_to_meeting']['median_seconds'], SLOW_STAGE_SECONDS)
        self.assertFalse(rows['confirmed_to_meeting']['is_bottleneck'])
        self.assertIsNone(result['bottleneck'])

    def test_return_stage_is_measured_for_a_borrowed_order(self):
        order, created_at = self.make_order(status='borrowed', trade_mode='borrow')
        self.add_event(order, to_status='confirmed', after=created_at + timedelta(hours=1))
        self.add_event(order, to_status='meeting', after=created_at + timedelta(days=1))
        self.add_event(
            order, to_status='borrowed', after=created_at + timedelta(days=1, hours=2),
        )
        self.add_event(order, to_status='returned', after=created_at + timedelta(days=9))
        result = build_order_stage_flow(days=30, now=self.now)

        rows = {row['key']: row for row in result['stage_rows']}
        self.assertEqual(rows['borrowed_to_returned']['sample_size'], 1)
        self.assertEqual(rows['borrowed_to_returned']['median_label'], '8 天')
        self.assertEqual(result['summary']['end_to_end_median_label'], '9 天')

    def test_cancelled_orders_do_not_reach_later_stages(self):
        """A cancelled order keeps the stages it passed but stops being measured."""
        order, created_at = self.make_order(status='cancelled')
        self.add_event(order, to_status='confirmed', after=created_at + timedelta(hours=3))
        result = build_order_stage_flow(days=30, now=self.now)

        rows = {row['key']: row for row in result['stage_rows']}
        self.assertEqual(rows['placed_to_confirmed']['sample_size'], 1)
        self.assertEqual(rows['placed_to_confirmed']['median_label'], '3 小时')
        # It never reached the handoff stage, so that stage stays censored.
        self.assertEqual(rows['confirmed_to_meeting']['sample_size'], 0)
        self.assertEqual(result['summary']['measured_orders'], 0)
        self.assertEqual(result['summary']['censored_orders'], 1)

    def test_out_of_order_event_is_counted_and_dropped(self):
        order, created_at = self.make_order()
        self.add_event(order, to_status='confirmed', after=created_at + timedelta(hours=1))
        self.add_event(order, to_status='meeting', after=created_at - timedelta(hours=5))
        result = build_order_stage_flow(days=30, now=self.now)

        self.assertEqual(result['summary']['dropped_samples'], 1)
        rows = {row['key']: row for row in result['stage_rows']}
        self.assertEqual(rows['confirmed_to_meeting']['sample_size'], 0)
        self.assertIn('已按异常数据剔除', ' '.join(result['recommendations']))

    # --- breakdowns ----------------------------------------------------

    def test_facet_rows_group_the_same_measurements_by_category_and_location(self):
        other_category = Category.objects.create(name='阶段测试分类乙')
        other_location = CampusLocation.objects.create(name='阶段测试地点乙')
        for _ in range(2):
            self.build_full_chain(
                confirm=timedelta(hours=4), category=other_category,
                location=other_location,
            )
        result = build_order_stage_flow(days=30, now=self.now)

        facets = {facet['key']: facet for facet in result['facet_rows']}
        self.assertEqual(set(facets), {'category', 'location'})

        category_rows = {row['name']: row for row in facets['category']['rows']}
        self.assertEqual(category_rows['阶段测试分类乙']['order_count'], 2)
        self.assertEqual(category_rows['阶段测试分类乙']['measured_orders'], 2)
        self.assertEqual(
            category_rows['阶段测试分类乙']['worst_stage_label'], '当面交付 → 成交 / 借出',
        )

        location_rows = {row['name']: row for row in facets['location']['rows']}
        self.assertEqual(location_rows['阶段测试地点乙']['order_count'], 2)

    def test_orders_without_a_location_are_left_out_of_the_location_breakdown(self):
        self.build_full_chain(confirm=timedelta(hours=2), location=None)
        result = build_order_stage_flow(days=30, now=self.now)

        location_facet = next(
            facet for facet in result['facet_rows'] if facet['key'] == 'location'
        )
        self.assertEqual(location_facet['rows'], [])

    def test_facet_rows_are_capped(self):
        for index in range(8):
            self.build_full_chain(
                confirm=timedelta(hours=1),
                title=f'阶段测试商品 {index}',
                location=CampusLocation.objects.create(name=f'阶段测试地点 {index}'),
            )
        result = build_order_stage_flow(days=30, now=self.now)

        location_facet = next(
            facet for facet in result['facet_rows'] if facet['key'] == 'location'
        )
        self.assertEqual(len(location_facet['rows']), 6)

    # --- recommendations ----------------------------------------------

    def test_recommendations_point_at_the_bottleneck_stage(self):
        for _ in range(3):
            self.build_full_chain(confirm=timedelta(minutes=2), meeting=timedelta(days=6))
        result = build_order_stage_flow(days=30, now=self.now)

        recommendations = ' '.join(result['recommendations'])
        self.assertIn('卖家确认 → 当面交付', recommendations)
        self.assertIn('优先缩短这一段', recommendations)

    def test_recommendations_mention_censored_orders(self):
        self.make_order(status='pending', created_ago=timedelta(days=3))
        result = build_order_stage_flow(days=30, now=self.now)

        self.assertIn('仍未走完所属流程', ' '.join(result['recommendations']))

    def test_recommendations_stay_positive_when_nothing_is_flagged(self):
        self.build_full_chain(confirm=timedelta(minutes=3), meeting=timedelta(hours=5),
                              complete=timedelta(hours=6))
        result = build_order_stage_flow(days=30, now=self.now)

        # The order reached its terminal stage, so no censoring is reported.
        self.assertEqual(result['summary']['censored_orders'], 0)
        self.assertEqual(
            result['recommendations'][-1], '当前周期各阶段耗时没有明显异常，继续保持。',
        )

    def test_empty_period_reports_no_data(self):
        result = build_order_stage_flow(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['summary']['total_orders'], 0)
        self.assertEqual(result['stage_max'], 1)
        self.assertTrue(result['recommendations'])