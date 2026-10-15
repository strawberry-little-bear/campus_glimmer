# -*- coding: utf-8 -*-
"""守住生命周期提醒跨学期对比的口径边界。

这些用例特别关注三件事：分母只能来自事件台账、观察窗口必须按阶段封闭、
刷新动作不能因为一个可覆盖时间戳而把旧学期伪装成效果变差。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .lifecycle_reminder_term_trend import (
    DEFAULT_LOOKBACK_DAYS,
    MIN_PHASE_ITEMS,
    PHASE_GAP_POINTS,
    build_lifecycle_reminder_term_trend,
    build_lifecycle_reminder_term_trend_dashboard,
)
from .models import (
    AcademicPhase,
    AcademicTerm,
    CampusLocation,
    Category,
    Item,
    LifecycleReminderEvent,
    Order,
    OrderEvent,
)


class LifecycleReminderTermTrendTests(TestCase):
    """同一阶段跨学期相比，不用相邻月份替代校园节律。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='term-trend-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='term-trend-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='跨学期分类')
        self.location = CampusLocation.objects.create(name='跨学期地点')
        AcademicTerm.objects.update(is_active=False)
        self.today = timezone.localdate()
        self.now = self.at(0)

    def at(self, days_ago, hour=12):
        day = self.today - timedelta(days=days_ago)
        value = timezone.datetime.combine(day, timezone.datetime.min.time())
        return timezone.make_aware(value.replace(hour=hour))

    def make_term(self, name, starts_on, *, phase='exam', start_offset=100, end_offset=110):
        term = AcademicTerm.objects.create(
            name=name,
            slug=name,
            kind='autumn',
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=140),
        )
        AcademicPhase.objects.create(
            term=term,
            phase=phase,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        return term

    def make_closed_terms(self):
        old = self.make_term('上一学期', self.today - timedelta(days=390))
        new = self.make_term('本学期', self.today - timedelta(days=220))
        return old, new

    def make_item(self, title, **kwargs):
        defaults = {
            'title': title,
            'description': '用于测试生命周期提醒的跨学期对比',
            'price': '35.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': self.seller,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def remind(self, item, at, kind='attention'):
        event = LifecycleReminderEvent.objects.create(
            item=item,
            seller=item.seller,
            kind=kind,
            sent_at=at,
        )
        return event

    def stamp_legacy(self, item, at):
        Item.objects.filter(pk=item.pk).update(attention_reminder_sent_at=at)
        return item

    def terminal(self, item, at, status='completed'):
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=item.seller,
            agreed_price=item.price,
            status=status,
        )
        event = OrderEvent.objects.create(order=order, to_status=status)
        OrderEvent.objects.filter(pk=event.pk).update(created_at=at)
        return order

    def report(self, **kwargs):
        kwargs.setdefault('now', self.now)
        return build_lifecycle_reminder_term_trend(**kwargs)

    def test_empty_ledger_reports_empty_state(self):
        report = self.report()

        self.assertFalse(report['has_data'])
        self.assertFalse(report['has_sample'])
        self.assertEqual(report['phase_groups'], [])
        self.assertEqual(report['no_term_events'], 0)
        self.assertEqual(report['unassigned_events'], 0)
        self.assertIn('没有生命周期提醒事件', report['summary'])

    def test_denominator_uses_only_the_event_ledger(self):
        _old, new = self.make_closed_terms()
        event_item = self.make_item('事件商品')
        legacy_item = self.make_item('旧时间戳商品')
        self.remind(event_item, self.at(116))
        self.stamp_legacy(legacy_item, self.at(116))

        group = self.report()['phase_groups'][0]

        self.assertEqual(group['current']['event_count'], 1)
        self.assertEqual(group['current']['item_count'], 1)
        self.assertEqual(group['current']['seller_count'], 1)

    def test_repeated_reminders_deduplicate_items_but_keep_event_scale(self):
        _old, new = self.make_closed_terms()
        item = self.make_item('重复提醒商品')
        self.remind(item, self.at(118), kind='attention')
        self.remind(item, self.at(114), kind='price')

        current = self.report()['phase_groups'][0]['current']

        self.assertEqual(current['event_count'], 2)
        self.assertEqual(current['item_count'], 1)
        self.assertEqual(current['repeat_reminder_rate'], 50.0)

    def test_phase_is_compared_only_after_its_window_closes(self):
        term = self.make_term(
            '未封闭学期', self.today - timedelta(days=30),
            start_offset=10, end_offset=20,
        )
        item = self.make_item('未封闭商品')
        self.remind(item, self.at(15))

        report = self.report()

        self.assertEqual(report['open_cell_events'], 1)
        self.assertEqual(report['closed_cell_count'], 0)
        self.assertEqual(report['phase_groups'], [])
        self.assertIn('还没有同学期阶段满 14 天观察窗口', report['summary'])

    def test_latest_reminder_and_fourteen_day_terminal_window_are_used(self):
        _old, new = self.make_closed_terms()
        item = self.make_item('最晚提醒商品')
        self.remind(item, self.at(118), kind='attention')
        self.remind(item, self.at(114), kind='price')
        self.terminal(item, self.at(113))

        current = self.report()['phase_groups'][0]['current']

        self.assertEqual(current['terminal_action_count'], 1)
        self.assertEqual(current['terminal_action_rate'], 100.0)

    def test_terminal_action_before_or_after_window_is_not_counted(self):
        _old, new = self.make_closed_terms()
        before = self.make_item('早于提醒')
        late = self.make_item('晚于窗口')
        self.remind(before, self.at(114))
        self.terminal(before, self.at(115))
        self.remind(late, self.at(114))
        self.terminal(late, self.at(98))

        current = self.report()['phase_groups'][0]['current']

        self.assertEqual(current['item_count'], 2)
        self.assertEqual(current['terminal_action_count'], 0)
        self.assertEqual(current['terminal_action_rate'], 0.0)

    def test_refresh_timestamp_does_not_enter_cross_term_direction(self):
        _old, new = self.make_closed_terms()
        item = self.make_item('只刷新商品')
        self.remind(item, self.at(114))
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=self.at(1), refresh_count=1,
        )

        current = self.report()['phase_groups'][0]['current']

        self.assertEqual(current['terminal_action_count'], 0)
        self.assertEqual(current['terminal_action_rate'], 0.0)

    def test_calendar_coverage_gaps_are_counted_without_joining_comparisons(self):
        self.make_closed_terms()
        unassigned_term = self.make_term(
            '只有开学周', self.today - timedelta(days=180),
            phase='registration', start_offset=0, end_offset=5,
        )
        unassigned = self.make_item('未划分阶段商品')
        outside = self.make_item('无学期商品')
        self.remind(unassigned, self.at(50))
        self.remind(outside, self.at(10))

        report = self.report()

        self.assertEqual(report['no_term_events'], 1)
        self.assertEqual(report['unassigned_events'], 1)
        self.assertEqual(report['phase_groups'], [])
        self.assertIn('无学期日历', report['summary'])
        self.assertIn('未划分阶段', report['summary'])

    def test_same_phase_reaches_a_direction_only_after_both_samples(self):
        old, new = self.make_closed_terms()
        for index in range(MIN_PHASE_ITEMS):
            item = self.make_item(f'本学期 {index}')
            self.remind(item, self.at(114))
            if index < 6:
                self.terminal(item, self.at(113))
        for index in range(MIN_PHASE_ITEMS):
            item = self.make_item(f'上一学期 {index}')
            self.remind(item, self.at(288))

        group = self.report()['phase_groups'][0]

        self.assertTrue(group['has_baseline'])
        self.assertTrue(group['has_sample'])
        self.assertEqual(group['current']['terminal_action_rate'], 50.0)
        self.assertEqual(group['previous']['terminal_action_rate'], 0.0)
        self.assertEqual(group['gap_points'], 50.0)
        self.assertEqual(group['direction'], 'rising')
        self.assertTrue(group['is_flagged'])

    def test_thin_cells_keep_figures_but_do_not_get_a_direction(self):
        old, new = self.make_closed_terms()
        for index in range(MIN_PHASE_ITEMS):
            item = self.make_item(f'足样本 {index}')
            self.remind(item, self.at(114))
            self.terminal(item, self.at(113))
        for index in range(MIN_PHASE_ITEMS - 1):
            item = self.make_item(f'薄样本 {index}')
            self.remind(item, self.at(288))

        group = self.report()['phase_groups'][0]

        self.assertFalse(group['has_sample'])
        self.assertEqual(group['gap_points'], 100.0)
        self.assertIsNone(group['direction'])
        self.assertFalse(group['is_flagged'])
        self.assertIn('不足', self.report()['summary'])

    def test_single_closed_cell_does_not_invent_a_previous_baseline(self):
        _old, new = self.make_closed_terms()
        item = self.make_item('首个格子')
        self.remind(item, self.at(114))

        group = self.report()['phase_groups'][0]

        self.assertFalse(group['has_baseline'])
        self.assertIsNone(group['previous'])
        self.assertIsNone(group['gap_points'])
        self.assertIsNone(group['direction'])
        self.assertIn('不虚构上一学期基线', self.report()['summary'])

    def test_dashboard_adapter_keeps_the_fixed_cross_term_lookback(self):
        report = build_lifecycle_reminder_term_trend_dashboard(days=7)

        self.assertEqual(report['days'], DEFAULT_LOOKBACK_DAYS)
        self.assertEqual(report['gap_points'], PHASE_GAP_POINTS)

    def test_dashboard_and_export_render_the_same_term_trend_contract(self):
        self.make_closed_terms()
        item = self.make_item('页面商品')
        self.remind(item, self.at(114))
        staff = User.objects.create_user(
            username='term-trend-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 7})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 7})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '生命周期提醒的跨学期终态转化')
        self.assertContains(page, '分母只读生命周期提醒事件台账')
        self.assertContains(page, '刷新时间会被下一次刷新覆盖')
        csv_text = export.content.decode('utf-8-sig')
        self.assertIn('生命周期提醒的跨学期终态转化', csv_text)
        self.assertIn('事件口径商品数', csv_text)
        self.assertIn('终态动作率（%）', csv_text)
        self.assertIn('无学期日历事件数', csv_text)
        self.assertIn('刷新时间会被下一次刷新覆盖', csv_text)
