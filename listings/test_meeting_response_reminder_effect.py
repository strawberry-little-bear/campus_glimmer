# -*- coding: utf-8 -*-
"""交付安排提醒效果面板的观察口径与页面/导出契约。"""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .meeting_response_reminders import send_meeting_response_reminders
from .meeting_response_reminder_effect import (
    _build_notice_action_rows,
    _build_notice_order_rows,
    build_meeting_response_reminder_effect,
)
from .models import (
    CampusLocation,
    Category,
    Item,
    MeetingAppointment,
    MeetingAppointmentEvent,
    MeetingResponseReminderEvent,
    Notification,
    Order,
)


class MeetingReminderFixtureMixin:
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.seller = User.objects.create_user(username='effect-seller')
        self.buyer = User.objects.create_user(username='effect-buyer')
        self.category = Category.objects.create(name='提醒效果测试分类')
        self.location = CampusLocation.objects.create(name='提醒效果测试地点')
        self.case_count = 0

    def make_case(self, *, recipient_role='seller', proposal_age_hours=25):
        self.case_count += 1
        proposer = self.buyer if recipient_role == 'seller' else self.seller
        item = Item.objects.create(
            title='提醒效果商品 %d' % self.case_count,
            description='用于观察提醒之后是否出现回应',
            price=Decimal('20.00'),
            status='available',
            seller=self.seller,
            category=self.category,
            location=self.location,
        )
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price='20.00',
            status='confirmed',
        )
        start_at = self.now + timedelta(days=1 + self.case_count)
        appointment = MeetingAppointment.objects.create(
            order=order,
            proposed_by=proposer,
            location=self.location,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )
        proposal = MeetingAppointmentEvent.objects.create(
            order=order,
            appointment=appointment,
            event_type='proposed',
            occurred_at=self.now - timedelta(hours=proposal_age_hours),
            actor=proposer,
            proposed_by=proposer,
            proposed_start_at=appointment.start_at,
            proposed_end_at=appointment.end_at,
            location=self.location,
            location_name=self.location.name,
            new_status='pending',
        )
        return {
            'item': item,
            'order': order,
            'appointment': appointment,
            'proposal': proposal,
            'recipient_role': recipient_role,
        }

    def send_cases(self, now=None):
        result = send_meeting_response_reminders(now=now or self.now)
        self.assertEqual(result['sent'], self.case_count)
        return result

    def add_action(self, case, event_type, *, hours_after):
        notification = Notification.objects.get(order=case['order'])
        occurred_at = notification.created_at + timedelta(hours=hours_after)
        recipient = self.seller if case['recipient_role'] == 'seller' else self.buyer
        if event_type == 'rescheduled':
            proposed_by = recipient
            new_status = 'pending'
            responded_by = None
            responded_at = None
        else:
            proposed_by = case['appointment'].proposed_by
            new_status = 'confirmed' if event_type == 'accepted' else 'declined'
            responded_by = recipient
            responded_at = occurred_at
        return MeetingAppointmentEvent.objects.create(
            order=case['order'],
            appointment=case['appointment'],
            event_type=event_type,
            occurred_at=occurred_at,
            actor=recipient,
            proposed_by=proposed_by,
            proposed_start_at=case['appointment'].start_at,
            proposed_end_at=case['appointment'].end_at,
            location=self.location,
            location_name=self.location.name,
            responded_by=responded_by,
            responded_at=responded_at,
            previous_status='pending',
            new_status=new_status,
        )

    def mark_read(self, case, *, hours_after):
        notification = Notification.objects.get(order=case['order'])
        notification.is_read = True
        notification.read_at = notification.created_at + timedelta(hours=hours_after)
        notification.save(update_fields=['is_read', 'read_at'])
        return notification


class MeetingResponseReminderEffectTests(MeetingReminderFixtureMixin, TestCase):
    def test_production_sender_creates_an_open_observation_sample(self):
        self.make_case()
        self.send_cases()

        effect = build_meeting_response_reminder_effect(
            days=30, now=self.now + timedelta(hours=2),
        )

        self.assertTrue(effect['has_data'])
        self.assertEqual(effect['sent_count'], 1)
        self.assertEqual(effect['notice_available_count'], 1)
        self.assertEqual(effect['read_notice_count'], 0)
        self.assertEqual(effect['open_count'], 1)
        self.assertEqual(effect['closed_count'], 0)
        self.assertEqual(effect['response_rate'], None)
        self.assertEqual(effect['notice_action_closed_count'], 0)
        self.assertEqual(effect['quality_status'], 'accumulating')
        self.assertEqual(MeetingResponseReminderEvent.objects.count(), 1)

    def test_read_and_accepted_action_use_observed_timestamps(self):
        case = self.make_case()
        self.send_cases()
        self.mark_read(case, hours_after=1)
        self.add_action(case, 'accepted', hours_after=2)

        effect = build_meeting_response_reminder_effect(
            days=30, now=self.now + timedelta(hours=3),
        )

        self.assertEqual(effect['read_notice_count'], 1)
        self.assertEqual(effect['notice_read_rate'], 100.0)
        self.assertEqual(effect['responded_count'], 1)
        self.assertEqual(effect['accepted_count'], 1)
        self.assertEqual(effect['closed_count'], 1)
        self.assertEqual(effect['response_rate'], 100.0)
        self.assertEqual(effect['median_response_hours'], 2.0)
        self.assertEqual(effect['open_count'], 0)
        rows = {row['key']: row for row in effect['notice_action_rows']}
        self.assertEqual(rows['read']['closed_count'], 1)
        self.assertEqual(rows['read']['action_count'], 1)
        self.assertEqual(rows['read']['no_action_count'], 0)
        self.assertEqual(rows['read']['action_rate'], 100.0)
        self.assertFalse(rows['read']['comparable'])
        self.assertEqual(effect['read_state_known_count'], 1)
        self.assertEqual(effect['notice_order_known_count'], 1)
        order_rows = {
            row['key']: row for row in effect['notice_order_rows']
        }
        self.assertEqual(order_rows['read_before_action']['count'], 1)

    def test_inconsistent_read_fields_are_excluded_from_unread_and_denominator(self):
        read_without_time = self.make_case(proposal_age_hours=25 + 5 * 24)
        time_without_read = self.make_case(proposal_age_hours=25 + 5 * 24)
        valid_read = self.make_case(proposal_age_hours=25 + 5 * 24)
        old_sent = self.now - timedelta(days=5)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)

        first_notification = Notification.objects.get(order=read_without_time['order'])
        first_notification.is_read = True
        first_notification.read_at = None
        first_notification.save(update_fields=['is_read', 'read_at'])
        second_notification = Notification.objects.get(order=time_without_read['order'])
        second_notification.is_read = False
        second_notification.read_at = old_sent + timedelta(hours=1)
        second_notification.save(update_fields=['is_read', 'read_at'])
        self.mark_read(valid_read, hours_after=1)
        self.add_action(read_without_time, 'accepted', hours_after=2)
        self.add_action(time_without_read, 'declined', hours_after=2)
        self.add_action(valid_read, 'accepted', hours_after=2)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)
        rows = {row['key']: row for row in effect['notice_action_rows']}

        self.assertEqual(effect['notice_available_count'], 3)
        self.assertEqual(effect['read_state_known_count'], 1)
        self.assertEqual(effect['read_notice_count'], 1)
        self.assertEqual(effect['inconsistent_read_state_count'], 2)
        self.assertEqual(effect['notice_read_rate'], 100.0)
        self.assertEqual(rows['read']['closed_count'], 1)
        self.assertEqual(rows['unread']['closed_count'], 0)
        self.assertEqual(effect['notice_action_closed_count'], 1)
        self.assertEqual(effect['notice_order_known_count'], 1)
        self.assertEqual(effect['quality_status'], 'needs_review')
        self.assertIn('阅读字段不一致 2 条', effect['quality_note'])

    def test_notice_order_rows_split_read_action_timing(self):
        read_first = self.make_case(proposal_age_hours=25 + 5 * 24)
        action_first = self.make_case(proposal_age_hours=25 + 5 * 24)
        same_time = self.make_case(proposal_age_hours=25 + 5 * 24)
        unread_action = self.make_case(proposal_age_hours=25 + 5 * 24)
        old_sent = self.now - timedelta(days=5)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)

        self.mark_read(read_first, hours_after=1)
        self.add_action(read_first, 'accepted', hours_after=2)
        self.add_action(action_first, 'declined', hours_after=2)
        self.mark_read(action_first, hours_after=3)
        self.mark_read(same_time, hours_after=2)
        self.add_action(same_time, 'rescheduled', hours_after=2)
        self.add_action(unread_action, 'accepted', hours_after=2)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)
        rows = {row['key']: row for row in effect['notice_order_rows']}

        self.assertEqual(effect['notice_order_known_count'], 4)
        self.assertEqual(set(rows), {
            'read_before_action', 'action_before_read', 'same_time', 'unread_action',
        })
        self.assertEqual(
            {key: row['count'] for key, row in rows.items()},
            {
                'read_before_action': 1,
                'action_before_read': 1,
                'same_time': 1,
                'unread_action': 1,
            },
        )
        self.assertTrue(all(row['share'] == 25.0 for row in rows.values()))
        self.assertIn('先后关系不等于因果', effect['notice_order_note'])

    def test_notice_order_table_needs_twelve_classified_actions(self):
        observations = [
            {
                'notice_state': 'read',
                'within_window': True,
                'read_at': self.now - timedelta(hours=2),
                'action_at': self.now - timedelta(hours=1),
            }
            for _index in range(11)
        ]

        rows = _build_notice_order_rows(observations)
        self.assertFalse(any(row['comparable'] for row in rows))

        observations.append({
            'notice_state': 'read',
            'within_window': True,
            'read_at': self.now - timedelta(hours=2),
            'action_at': self.now - timedelta(hours=1),
        })
        rows = _build_notice_order_rows(observations)
        self.assertTrue(all(row['comparable'] for row in rows))

    def test_notice_action_rows_split_all_four_read_and_action_cells(self):
        read_acted = self.make_case(proposal_age_hours=25 + 5 * 24)
        read_not_acted = self.make_case(proposal_age_hours=25 + 5 * 24)
        unread_acted = self.make_case(proposal_age_hours=25 + 5 * 24)
        unread_not_acted = self.make_case(proposal_age_hours=25 + 5 * 24)
        old_sent = self.now - timedelta(days=5)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)

        self.mark_read(read_acted, hours_after=1)
        self.mark_read(read_not_acted, hours_after=1)
        self.add_action(read_acted, 'accepted', hours_after=2)
        self.add_action(unread_acted, 'declined', hours_after=3)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)
        rows = {row['key']: row for row in effect['notice_action_rows']}

        expected = {
            'read': (2, 1, 1, 50.0),
            'unread': (2, 1, 1, 50.0),
        }
        for key, (closed, action_count, no_action_count, action_rate) in expected.items():
            self.assertEqual(rows[key]['closed_count'], closed)
            self.assertEqual(rows[key]['action_count'], action_count)
            self.assertEqual(rows[key]['no_action_count'], no_action_count)
            self.assertEqual(rows[key]['action_rate'], action_rate)
            self.assertFalse(rows[key]['comparable'])
        self.assertEqual(effect['notice_action_closed_count'], 4)
        self.assertEqual(effect['quality_label'], '数据积累中')

    def test_actions_are_split_by_role_and_type(self):
        accepted_case = self.make_case(recipient_role='seller')
        declined_case = self.make_case(recipient_role='buyer')
        rescheduled_case = self.make_case(recipient_role='seller')
        self.send_cases()
        self.add_action(accepted_case, 'accepted', hours_after=1)
        self.add_action(declined_case, 'declined', hours_after=4)
        self.add_action(rescheduled_case, 'rescheduled', hours_after=7)

        effect = build_meeting_response_reminder_effect(
            days=30, now=self.now + timedelta(hours=8),
        )
        rows = {row['key']: row for row in effect['role_rows']}

        self.assertEqual(rows['buyer']['sent_count'], 1)
        self.assertEqual(rows['buyer']['declined_count'], 1)
        self.assertEqual(rows['buyer']['response_rate'], 100.0)
        self.assertEqual(rows['seller']['sent_count'], 2)
        self.assertEqual(rows['seller']['accepted_count'], 1)
        self.assertEqual(rows['seller']['rescheduled_count'], 1)
        self.assertEqual(effect['accepted_count'], 1)
        self.assertEqual(effect['declined_count'], 1)
        self.assertEqual(effect['rescheduled_count'], 1)

    def test_old_unanswered_send_is_closed_and_not_called_a_failure_direction(self):
        old_sent = self.now - timedelta(days=5)
        self.make_case(proposal_age_hours=25 + 5 * 24)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)

        self.assertEqual(effect['closed_count'], 1)
        self.assertEqual(effect['no_action_count'], 1)
        self.assertEqual(effect['responded_count'], 0)
        self.assertEqual(effect['response_rate'], 0.0)
        self.assertFalse(effect['comparable'])
        self.assertIn('只报告计数与时延', effect['summary'])

    def test_action_after_window_is_late_and_not_counted_in_rate(self):
        old_sent = self.now - timedelta(days=5)
        case = self.make_case(proposal_age_hours=25 + 5 * 24)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)
        self.add_action(case, 'accepted', hours_after=80)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)

        self.assertEqual(effect['closed_count'], 1)
        self.assertEqual(effect['late_action_count'], 1)
        self.assertEqual(effect['responded_count'], 0)
        self.assertEqual(effect['response_rate'], 0.0)
        self.assertEqual(effect['no_action_count'], 0)
        rows = {row['key']: row for row in effect['notice_action_rows']}
        self.assertEqual(rows['unread']['closed_count'], 1)
        self.assertEqual(rows['unread']['action_count'], 0)
        self.assertEqual(rows['unread']['no_action_count'], 1)
        self.assertEqual(rows['unread']['action_rate'], 0.0)
        self.assertEqual(effect['notice_order_known_count'], 0)

    def test_deleted_notification_keeps_send_but_stays_out_of_unread_cross(self):
        case = self.make_case(proposal_age_hours=25 + 5 * 24)
        old_sent = self.now - timedelta(days=5)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)
        Notification.objects.get(order=case['order']).delete()

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)

        self.assertEqual(effect['sent_count'], 1)
        self.assertEqual(effect['notice_available_count'], 0)
        self.assertEqual(effect['missing_notice_count'], 1)
        self.assertEqual(effect['closed_count'], 1)
        self.assertEqual(effect['no_action_count'], 1)
        self.assertEqual(effect['open_count'], 0)
        self.assertEqual(effect['notice_action_closed_count'], 0)
        self.assertEqual(effect['quality_status'], 'needs_review')
        rows = {row['key']: row for row in effect['notice_action_rows']}
        self.assertEqual(rows['read']['closed_count'], 0)
        self.assertEqual(rows['unread']['closed_count'], 0)
        self.assertIsNone(MeetingResponseReminderEvent.objects.get().notification)

    def test_each_cross_cell_needs_twelve_samples_before_direction_is_shown(self):
        observations = [
            {
                'notice_state': 'read', 'status': 'responded',
                'within_window': True,
            }
            for _index in range(12)
        ]
        observations.append({
            'notice_state': 'unread', 'status': 'open', 'within_window': False,
        })

        rows = {row['key']: row for row in _build_notice_action_rows(observations)}

        self.assertEqual(rows['read']['closed_count'], 12)
        self.assertEqual(rows['read']['action_count'], 12)
        self.assertEqual(rows['read']['no_action_count'], 0)
        self.assertFalse(rows['read']['comparable'])
        self.assertEqual(rows['unread']['closed_count'], 0)
        self.assertFalse(rows['unread']['comparable'])

    def test_legacy_notice_is_counted_separately_and_never_becomes_a_sample(self):
        case = self.make_case()
        Notification.objects.create(
            recipient=self.seller,
            actor=self.buyer,
            order=case['order'],
            item=case['item'],
            kind='meeting_response_pending',
            title='升级前通知',
            message='没有提醒台账',
            dedupe_key='meeting-response:legacy-effect',
        )

        effect = build_meeting_response_reminder_effect(
            days=30, now=self.now + timedelta(hours=1),
        )

        self.assertFalse(effect['has_data'])
        self.assertEqual(effect['sent_count'], 0)
        self.assertEqual(effect['legacy_notice_count'], 1)


class MeetingResponseReminderEffectSurfaceTests(
    MeetingReminderFixtureMixin, TestCase,
):
    """页面、懒加载分区和 CSV 读取同一份效果构建结果。"""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            username='effect-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_dashboard_renders_the_read_only_panel_without_user_identity(self):
        self.make_case(recipient_role='seller')
        self.send_cases()

        response = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        )
        html = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="ops-meeting-reminders"', html)
        self.assertIn('交付安排提醒触达与回应', html)
        self.assertIn('按接收角色观察', html)
        self.assertIn('阅读与窗口动作交叉观察', html)
        self.assertIn('已读与窗口动作时点', html)
        self.assertIn('先读后动', html)
        self.assertIn('先动后读', html)
        self.assertIn('同一时刻', html)
        self.assertIn('未读行动', html)
        self.assertIn('少于 12 条，只报数', html)
        self.assertIn('样本质量', html)
        self.assertIn('数据积累中', html)
        self.assertIn('观察状态与缺失', html)
        self.assertIn('窗口内回应时延', html)
        self.assertIn('等待中的样本不会被当成提醒失败', html)
        self.assertNotIn(self.seller.username, html)
        self.assertNotIn(self.buyer.username, html)

    def test_fragment_renders_the_same_panel_without_the_surrounding_section(self):
        self.make_case(recipient_role='buyer')
        self.send_cases()

        response = self.client.get(
            reverse(
                'operations_dashboard_fragment',
                kwargs={'anchor': 'ops-meeting-reminders'},
            ),
            {'days': 30},
        )
        html = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('交付安排提醒触达与回应', html)
        self.assertIn('meeting-response-reminder-effect-panel', html)
        self.assertNotIn('id="ops-meeting-reminders"', html)

    def test_export_uses_the_same_sent_count_and_table_shape(self):
        self.make_case(recipient_role='seller')
        self.send_cases()

        response = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )
        text = response.content.decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))

        self.assertEqual(response.status_code, 200)
        start = next(
            index for index, row in enumerate(rows)
            if row and row[0] == '交付安排提醒触达与回应'
        )
        section = rows[start:]
        summary = next(
            row for row in section
            if row and row[0].startswith('周期天数')
        )
        pairs = dict(zip(summary[::2], summary[1::2]))
        self.assertEqual(pairs['发送台账'], '1')
        self.assertEqual(pairs['观察窗口（小时）'], '72')
        self.assertEqual(pairs['最小关闭样本'], '12')
        self.assertEqual(pairs['阅读状态可判定'], '1')
        self.assertEqual(pairs['阅读字段不一致'], '0')

        role_header = next(
            index for index, row in enumerate(section)
            if row and row[0] == '接收角色'
        )
        self.assertEqual(section[role_header], [
            '接收角色', '发送', '通知可用', '阅读可判定', '已读', '字段异常',
            '通知阅读率（%）', '窗口内回应', '接受', '拒绝', '再次改期',
            '窗口回应率（%）', '中位回应时延', '样本',
        ])
        seller_row = next(
            row for row in section[role_header + 1:]
            if row and row[0] == '卖家'
        )
        self.assertEqual(seller_row[1], '1')

        cross_header = next(
            index for index, row in enumerate(section)
            if row and row[0] == '阅读状态'
        )
        self.assertEqual(section[cross_header], [
            '阅读状态', '已过窗样本', '窗口内行动', '窗口内未行动',
            '窗口回应率（%）', '样本',
        ])
        self.assertEqual(section[cross_header + 1], [
            '已读', '0', '0', '0', '—', '少于 12 条，只报数',
        ])

        order_header = next(
            index for index, row in enumerate(section)
            if row and row[0] == '时点关系'
        )
        self.assertEqual(section[order_header], [
            '时点关系', '窗口内行动', '占可判定行动（%）', '样本',
        ])
        self.assertEqual(section[order_header + 1], [
            '先读后动', '0', '—', '少于 12 条，只报数',
        ])
        self.assertIn('样本质量', text)
        self.assertIn('先后关系不等于因果', text)
        self.assertIn('不能证明由提醒引起', text)
