# -*- coding: utf-8 -*-
"""交付安排提醒效果面板的观察口径与页面/导出契约。"""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import MeetingResponseReminderReviewForm
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
    MeetingResponseReminderReview,
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

    def review_case(
        self, case, reviewer, *, outcome='waiting', note='已核实，继续观察。',
        data_issue_reason='', reviewed_at=None,
    ):
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )
        return MeetingResponseReminderReview.objects.create(
            reminder=reminder,
            outcome=outcome,
            data_issue_reason=data_issue_reason,
            note=note,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at or timezone.now(),
        )


class MeetingResponseReminderEffectTests(MeetingReminderFixtureMixin, TestCase):
    def test_production_sender_creates_an_open_observation_sample(self):
        case = self.make_case()
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
        self.assertEqual(effect['pending_review_count'], 1)
        self.assertEqual(effect['pending_review_overflow_count'], 0)
        self.assertEqual(len(effect['pending_review_rows']), 1)
        queue_row = effect['pending_review_rows'][0]
        self.assertEqual(queue_row['order_id'], case['order'].id)
        self.assertEqual(queue_row['role_label'], '卖家')
        self.assertEqual(queue_row['location_label'], self.location.name)
        self.assertEqual(queue_row['notice_label'], '未读')
        self.assertEqual(queue_row['status'], 'open')
        self.assertEqual(queue_row['status_label'], '观察中，尚无动作')
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
        self.assertEqual(effect['pending_review_count'], 0)
        self.assertEqual(effect['pending_review_rows'], [])

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

    def test_pending_review_prioritizes_overdue_then_longest_open(self):
        def create_and_send(*, proposal_age_hours, sent_at):
            case = self.make_case(proposal_age_hours=proposal_age_hours)
            with patch('django.utils.timezone.now', return_value=sent_at):
                result = send_meeting_response_reminders(now=sent_at)
            self.assertEqual(result['sent'], 1)
            return case

        oldest_overdue = create_and_send(
            proposal_age_hours=25 + 5 * 24,
            sent_at=self.now - timedelta(days=5),
        )
        newer_overdue = create_and_send(
            proposal_age_hours=25 + 4 * 24,
            sent_at=self.now - timedelta(days=4),
        )
        longest_open = create_and_send(
            proposal_age_hours=35,
            sent_at=self.now - timedelta(hours=10),
        )
        responded = create_and_send(
            proposal_age_hours=34,
            sent_at=self.now - timedelta(hours=9),
        )
        late_action = create_and_send(
            proposal_age_hours=25 + 6 * 24,
            sent_at=self.now - timedelta(days=6),
        )
        self.add_action(responded, 'accepted', hours_after=2)
        self.add_action(late_action, 'declined', hours_after=80)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)

        self.assertEqual(effect['pending_review_count'], 3)
        rows = effect['pending_review_all_rows']
        self.assertEqual(
            [row['order_id'] for row in rows],
            [
                oldest_overdue['order'].id,
                newer_overdue['order'].id,
                longest_open['order'].id,
            ],
        )
        self.assertEqual(
            [row['status'] for row in rows],
            ['no_action', 'no_action', 'open'],
        )
        self.assertEqual(effect['responded_count'], 1)
        self.assertEqual(effect['late_action_count'], 1)
        self.assertNotIn(responded['order'].id, [row['order_id'] for row in rows])
        self.assertNotIn(late_action['order'].id, [row['order_id'] for row in rows])

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
        self.assertEqual(effect['pending_review_count'], 1)
        self.assertEqual(effect['pending_review_rows'][0]['status'], 'no_action')
        self.assertEqual(
            effect['pending_review_rows'][0]['status_label'],
            '已过窗，尚无动作',
        )

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
        self.assertEqual(effect['pending_review_count'], 0)

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

    def test_dashboard_limits_pending_review_but_csv_keeps_the_full_list(self):
        old_sent = self.now - timedelta(days=5)
        cases = [
            self.make_case(proposal_age_hours=25 + 5 * 24)
            for _index in range(21)
        ]
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)

        effect = build_meeting_response_reminder_effect(days=30, now=self.now)
        self.assertEqual(effect['pending_review_count'], 21)
        self.assertEqual(len(effect['pending_review_rows']), 20)
        self.assertEqual(effect['pending_review_overflow_count'], 1)

        response = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        )
        html = response.content.decode('utf-8')
        panel_start = html.index('meeting-response-pending-review')
        panel = html[panel_start:html.index('按接收角色观察', panel_start)]
        linked_ids = [
            case['order'].id for case in cases
            if 'href="%s"' % reverse('order_detail', args=[case['order'].id]) in panel
        ]
        self.assertEqual(len(linked_ids), 20)
        self.assertEqual(linked_ids, [case['order'].id for case in cases[:20]])
        self.assertIn(
            '页面展示当前筛选前 20 条；CSV 导出当前筛选全部 21 条，'
            '其中另外 1 条只出现在导出中。',
            html,
        )

        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        start = rows.index(['提醒后待人工核对'])
        header_index = next(
            index for index in range(start, len(rows))
            if rows[index] and rows[index][0] == '订单编号'
        )
        self.assertEqual(rows[header_index], [
            '订单编号', '订单详情路径', '接收角色', '提议发起时间',
            '当前提议开始', '当前提议结束', '地点', '提醒发送时间',
            '提议已等待', '提醒后已过', '阅读状态', '观察状态',
            '复核结论', '数据异常类型', '复核时间', '复核说明',
            '复核历史次数',
        ])
        detail_rows = []
        for row in rows[header_index + 1:]:
            if not row:
                break
            detail_rows.append(row)
        self.assertEqual(len(detail_rows), 21)
        self.assertEqual(
            [int(row[0]) for row in detail_rows],
            [case['order'].id for case in cases],
        )
        self.assertEqual(
            detail_rows[0][1],
            reverse('order_detail', args=[cases[0]['order'].id]),
        )
        self.assertEqual(detail_rows[0][11], '已过窗，尚无动作')
        self.assertEqual(detail_rows[0][12], '未核对')
        self.assertEqual(detail_rows[0][13], '')
        self.assertEqual(detail_rows[0][14], '—')
        self.assertEqual(detail_rows[0][15], '')
        self.assertEqual(detail_rows[0][16], '0')

    def test_dashboard_renders_the_review_panel_without_user_identity(self):
        case = self.make_case(recipient_role='seller')
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
        self.assertIn('提醒后待人工核对', html)
        self.assertIn('观察中，尚无动作', html)
        self.assertIn(
            'href="%s"' % reverse('order_detail', args=[case['order'].id]),
            html,
        )
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
        case = self.make_case(recipient_role='seller')
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
        pending_header = next(
            index for index, row in enumerate(section)
            if row and row[0] == '订单编号'
        )
        self.assertEqual(section[pending_header][0:4], [
            '订单编号', '订单详情路径', '接收角色', '提议发起时间',
        ])
        self.assertEqual(section[pending_header + 1][0], str(case['order'].id))
        self.assertEqual(
            section[pending_header + 1][1],
            reverse('order_detail', args=[case['order'].id]),
        )
        self.assertEqual(section[pending_header + 1][10], '未读')
        self.assertEqual(section[pending_header + 1][11], '观察中，尚无动作')
        self.assertIn('样本质量', text)
        self.assertIn('先后关系不等于因果', text)
        self.assertIn('不能证明由提醒引起', text)
        self.assertIn('窗后动作（不进入清单）', text)

class MeetingResponseReminderReviewLedgerTests(
    MeetingReminderFixtureMixin, TestCase,
):
    """人工结论按最新记录筛选，每次提交保留独立历史。"""

    def setUp(self):
        super().setUp()
        self.reviewer = User.objects.create_user(username='review-ledger-staff')
        self.evaluation_now = timezone.now() + timedelta(hours=1)

    def test_each_outcome_filters_only_the_latest_pending_review(self):
        outcomes = ('waiting', 'resolved', 'data_issue', 'no_followup')
        cases = {}
        for index, outcome in enumerate(outcomes):
            case = self.make_case()
            cases[outcome] = case
        self.send_cases()
        for index, outcome in enumerate(outcomes):
            self.review_case(
                cases[outcome], self.reviewer, outcome=outcome,
                data_issue_reason=(
                    'reminder_mismatch' if outcome == 'data_issue' else ''
                ),
                note='人工依据 %s' % outcome,
                reviewed_at=self.now + timedelta(minutes=index),
            )

        all_effect = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now,
        )
        self.assertEqual(all_effect['pending_review_total_count'], 4)
        self.assertEqual(all_effect['pending_review_unreviewed_count'], 0)
        self.assertEqual(all_effect['pending_review_reviewed_count'], 4)
        for outcome in outcomes:
            self.assertEqual(
                all_effect['pending_review_%s_count' % outcome], 1,
            )
            effect = build_meeting_response_reminder_effect(
                days=30, now=self.evaluation_now, review_status=outcome,
            )
            self.assertEqual(effect['pending_review_count'], 1)
            self.assertEqual(
                effect['pending_review_all_rows'][0]['order_id'],
                cases[outcome]['order'].id,
            )
            self.assertEqual(effect['review_status'], outcome)
        self.assertEqual(
            [row['key'] for row in all_effect['review_filter_options']],
            ['unreviewed', 'waiting', 'resolved', 'data_issue', 'no_followup', 'all'],
        )

    def test_new_review_supersedes_filter_status_without_deleting_history(self):
        case = self.make_case()
        self.send_cases()
        first = self.review_case(
            case, self.reviewer, outcome='waiting', note='第一次继续等待',
            reviewed_at=self.now + timedelta(minutes=1),
        )
        second = self.review_case(
            case, self.reviewer, outcome='resolved', note='',
            reviewed_at=self.now + timedelta(minutes=2),
        )

        waiting = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now, review_status='waiting',
        )
        resolved = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now, review_status='resolved',
        )

        self.assertEqual(waiting['pending_review_count'], 0)
        self.assertEqual(resolved['pending_review_count'], 1)
        row = resolved['pending_review_all_rows'][0]
        self.assertEqual(row['review_outcome'], 'resolved')
        self.assertEqual(row['review_outcome_label'], '已处理')
        self.assertEqual(row['review_note'], '')
        self.assertEqual(
            MeetingResponseReminderReview.objects.filter(
                reminder=second.reminder,
            ).count(),
            2,
        )
        self.assertEqual(first.reviewed_at.minute + 1, second.reviewed_at.minute)

    def test_review_history_keeps_all_entries_newest_first(self):
        case = self.make_case()
        self.send_cases()
        first = self.review_case(
            case, self.reviewer, outcome='waiting',
            note='第一次核验仍等待。',
            reviewed_at=self.now + timedelta(minutes=1),
        )
        second = self.review_case(
            case, self.reviewer, outcome='data_issue',
            data_issue_reason='reminder_mismatch',
            note='再次核验发现提醒台账不一致。',
            reviewed_at=self.now + timedelta(minutes=2),
        )

        effect = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now, review_status='data_issue',
        )
        row = effect['pending_review_all_rows'][0]

        self.assertEqual(row['review_history_count'], 2)
        self.assertEqual(
            [entry['outcome'] for entry in row['review_history']],
            ['data_issue', 'waiting'],
        )
        self.assertEqual(
            [entry['outcome_label'] for entry in row['review_history']],
            ['数据异常', '继续等待'],
        )
        self.assertEqual(
            [entry['data_issue_reason'] for entry in row['review_history']],
            ['reminder_mismatch', ''],
        )
        self.assertEqual(
            row['review_history'][1]['reviewed_at_label'],
            timezone.localtime(first.reviewed_at).strftime('%Y-%m-%d %H:%M'),
        )
        self.assertEqual(
            row['review_history'][0]['reviewed_at_label'],
            timezone.localtime(second.reviewed_at).strftime('%Y-%m-%d %H:%M'),
        )
        self.assertEqual(row['data_issue_reason'], 'reminder_mismatch')
        self.assertEqual(row['data_issue_reason_label'], '提醒台账不一致')
        self.assertEqual(row['review_outcome'], 'data_issue')

    def test_unknown_filter_falls_back_to_unreviewed(self):
        self.make_case()
        self.send_cases()
        effect = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now, review_status='not-a-filter',
        )
        self.assertEqual(effect['review_status'], 'unreviewed')
        self.assertEqual(effect['review_status_label'], '未核对')
        self.assertEqual(effect['pending_review_count'], 1)
        self.assertEqual(effect['pending_review_all_rows'][0]['review_outcome'], '')

    def test_filtering_review_does_not_change_effect_metrics(self):
        case = self.make_case()
        self.send_cases()
        self.mark_read(case, hours_after=1)
        before = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now,
        )
        self.review_case(case, self.reviewer)
        after = build_meeting_response_reminder_effect(
            days=30, now=self.evaluation_now, review_status='waiting',
        )

        for key in (
            'sent_count', 'notice_available_count', 'read_notice_count',
            'read_state_known_count', 'notice_read_rate', 'responded_count',
            'accepted_count', 'declined_count', 'rescheduled_count',
            'no_action_count', 'open_count', 'late_action_count',
            'closed_count', 'quality_status', 'notice_action_rows',
            'notice_order_rows',
        ):
            self.assertEqual(after[key], before[key], key)

    def test_form_and_model_keep_review_requirements_and_history_immutable(self):
        case = self.make_case()
        self.send_cases()
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )

        missing_note = MeetingResponseReminderReviewForm(data={
            'outcome': 'waiting', 'note': '   ',
        })
        self.assertFalse(missing_note.is_valid())
        self.assertIn('note', missing_note.errors)

        missing_reason = MeetingResponseReminderReviewForm(data={
            'outcome': 'data_issue', 'note': '历史链缺少一条提议。',
        })
        self.assertFalse(missing_reason.is_valid())
        self.assertIn('data_issue_reason', missing_reason.errors)

        resolved = MeetingResponseReminderReviewForm(data={
            'outcome': 'resolved', 'note': '',
            'data_issue_reason': 'other',
        })
        self.assertTrue(resolved.is_valid(), resolved.errors)
        self.assertEqual(resolved.cleaned_data['data_issue_reason'], '')
        review = resolved.save(commit=False)
        review.reminder = reminder
        review.reviewed_by = self.reviewer
        review.save()
        review.note = '试图覆盖审计历史'
        with self.assertRaises(ValidationError):
            review.save()

        too_long = MeetingResponseReminderReviewForm(data={
            'outcome': 'waiting', 'note': '字' * 1001,
        })
        self.assertFalse(too_long.is_valid())
        self.assertIn('note', too_long.errors)


class MeetingResponseReminderReviewSurfaceTests(
    MeetingReminderFixtureMixin, TestCase,
):
    """运营页面的复核提交、权限、筛选和导出契约。"""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            username='review-surface-staff', password='safe-password-123',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def post_review(
        self, reminder, *, outcome='waiting', note='已核实，继续观察。',
        data_issue_reason='', days=30, review_status='unreviewed',
    ):
        return self.client.post(
            reverse(
                'review_meeting_response_reminder',
                kwargs={'reminder_id': reminder.id},
            ),
            {
                'outcome': outcome,
                'data_issue_reason': data_issue_reason,
                'note': note,
                'days': days,
                'review_status': review_status,
            },
        )

    def test_review_post_requires_staff_and_post(self):
        case = self.make_case()
        self.send_cases()
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )
        url = reverse(
            'review_meeting_response_reminder',
            kwargs={'reminder_id': reminder.id},
        )

        self.client.logout()
        anonymous = self.client.post(url, {
            'outcome': 'waiting', 'note': '继续等待', 'days': 30,
        })
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse('login'), anonymous['Location'])

        self.client.force_login(self.buyer)
        forbidden = self.client.post(url, {
            'outcome': 'waiting', 'note': '继续等待', 'days': 30,
        })
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(MeetingResponseReminderReview.objects.count(), 0)

        self.client.force_login(self.staff)
        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 302)
        self.assertEqual(get_response['Location'], reverse('operations_dashboard'))

    def test_staff_review_appends_history_and_preserves_business_state(self):
        case = self.make_case(recipient_role='seller')
        self.send_cases()
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )
        before = {
            'order': Order.objects.filter(pk=case['order'].pk).values().get(),
            'appointment': MeetingAppointment.objects.filter(
                pk=case['appointment'].pk,
            ).values().get(),
            'notification': Notification.objects.filter(
                order=case['order'],
            ).values().get(),
            'reminder': MeetingResponseReminderEvent.objects.filter(
                pk=reminder.pk,
            ).values().get(),
            'order_event_count': case['order'].events.count(),
        }

        response = self.post_review(
            reminder, outcome='waiting', note='已联系双方，继续等待。',
            review_status='waiting',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('review_status=waiting', response['Location'])
        self.assertIn('#ops-meeting-reminders', response['Location'])
        first = MeetingResponseReminderReview.objects.get()
        self.assertEqual(first.reviewed_by, self.staff)
        self.assertEqual(first.outcome, 'waiting')
        self.assertIsNotNone(first.reviewed_at)

        after = {
            'order': Order.objects.filter(pk=case['order'].pk).values().get(),
            'appointment': MeetingAppointment.objects.filter(
                pk=case['appointment'].pk,
            ).values().get(),
            'notification': Notification.objects.filter(
                order=case['order'],
            ).values().get(),
            'reminder': MeetingResponseReminderEvent.objects.filter(
                pk=reminder.pk,
            ).values().get(),
            'order_event_count': case['order'].events.count(),
        }
        self.assertEqual(after, before)

        self.post_review(
            reminder, outcome='resolved', note='', data_issue_reason='other',
            review_status='resolved',
        )
        resolved_review = MeetingResponseReminderReview.objects.get(
            reminder=reminder, outcome='resolved',
        )
        self.assertEqual(resolved_review.data_issue_reason, '')
        self.post_review(
            reminder, outcome='data_issue',
            data_issue_reason='reminder_mismatch',
            note='门店地址与后台档案不一致。', review_status='data_issue',
        )
        self.assertEqual(
            MeetingResponseReminderReview.objects.filter(reminder=reminder).count(),
            3,
        )

        page = self.client.get(
            reverse('operations_dashboard'),
            {'days': 30, 'review_status': 'data_issue'},
        )
        html = page.content.decode('utf-8')
        self.assertIn('门店地址与后台档案不一致。', html)
        self.assertIn('提醒台账不一致', html)
        self.assertIn('查看完整复核历史（3 条）', html)
        reviewed_at = MeetingResponseReminderReview.objects.order_by(
            '-reviewed_at', '-pk',
        ).first().reviewed_at
        self.assertIn(
            timezone.localtime(reviewed_at).strftime('%Y-%m-%d %H:%M'), html,
        )
        self.assertNotIn(self.seller.username, html)
        self.assertNotIn(self.buyer.username, html)
        fragment = self.client.get(
            reverse(
                'operations_dashboard_fragment',
                kwargs={'anchor': 'ops-meeting-reminders'},
            ),
            {'days': 30, 'review_status': 'data_issue'},
        )
        fragment_html = fragment.content.decode('utf-8')
        for username in (self.staff.username, self.seller.username, self.buyer.username):
            self.assertNotIn(username, fragment_html)

    def test_invalid_review_and_changed_reminder_write_no_history(self):
        case = self.make_case()
        self.send_cases()
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )

        invalid_outcome = self.post_review(
            reminder, outcome='made_up', note='无效结论',
        )
        missing_note = self.post_review(reminder, outcome='no_followup', note='')
        missing_reason = self.post_review(
            reminder, outcome='data_issue', note='缺少结构化原因。',
        )
        self.assertEqual(invalid_outcome.status_code, 302)
        self.assertEqual(missing_note.status_code, 302)
        self.assertEqual(missing_reason.status_code, 302)
        self.assertEqual(MeetingResponseReminderReview.objects.count(), 0)

        self.add_action(case, 'accepted', hours_after=2)
        changed = self.post_review(reminder, outcome='waiting', note='继续等待')
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(MeetingResponseReminderReview.objects.count(), 0)

    def test_review_rejects_a_reminder_outside_the_requested_period(self):
        old_sent = self.now - timedelta(days=5)
        case = self.make_case(proposal_age_hours=25 + 5 * 24)
        with patch('django.utils.timezone.now', return_value=old_sent):
            self.send_cases(now=old_sent)
        reminder = MeetingResponseReminderEvent.objects.get(
            proposal_event=case['proposal'],
        )

        response = self.post_review(
            reminder, outcome='waiting', note='周期外复核', days=1,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MeetingResponseReminderReview.objects.count(), 0)

    def test_page_fragment_and_csv_use_the_current_review_filter(self):
        waiting_case = self.make_case()
        issue_case = self.make_case()
        self.send_cases()
        self.review_case(
            waiting_case, self.staff, outcome='waiting',
            note='窗口内仍需继续观察。',
        )
        self.review_case(
            issue_case, self.staff, outcome='data_issue',
            data_issue_reason='notification_missing',
            note='通知关联存在异常。',
        )

        page = self.client.get(
            reverse('operations_dashboard'),
            {'days': 30, 'review_status': 'waiting'},
        )
        html = page.content.decode('utf-8')
        panel_start = html.index('meeting-response-pending-review')
        panel = html[panel_start:html.index('按接收角色观察', panel_start)]
        self.assertIn(
            'href="%s"' % reverse('order_detail', args=[waiting_case['order'].id]),
            panel,
        )
        self.assertNotIn(
            'href="%s"' % reverse('order_detail', args=[issue_case['order'].id]),
            panel,
        )
        self.assertIn('窗口内仍需继续观察。', panel)
        self.assertIn('当前筛选 1 条 · 全部待核对 2 条', html)
        issue_page = self.client.get(
            reverse('operations_dashboard'),
            {'days': 30, 'review_status': 'data_issue'},
        ).content.decode('utf-8')
        self.assertIn('通知记录缺失', issue_page)

        fragment = self.client.get(
            reverse(
                'operations_dashboard_fragment',
                kwargs={'anchor': 'ops-meeting-reminders'},
            ),
            {'days': 30, 'review_status': 'waiting'},
        )
        fragment_html = fragment.content.decode('utf-8')
        self.assertIn(
            'href="%s"' % reverse('order_detail', args=[waiting_case['order'].id]),
            fragment_html,
        )
        self.assertNotIn('href="%s"' % reverse('order_detail', args=[issue_case['order'].id]), fragment_html)
        self.assertNotIn('通知关联存在异常。', fragment_html)

        export = self.client.get(
            reverse('operations_dashboard_export'),
            {'days': 30, 'review_status': 'waiting'},
        )
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        filter_summary = next(
            row for row in rows if row and row[0] == '当前核对筛选'
        )
        self.assertEqual(filter_summary[:6], [
            '当前核对筛选', '继续等待', '当前筛选数量', '1',
            '全部待核对', '2',
        ])
        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == '订单编号'
        )
        self.assertEqual(rows[header_index][-5:], [
            '复核结论', '数据异常类型', '复核时间', '复核说明',
            '复核历史次数',
        ])
        detail = rows[header_index + 1]
        self.assertEqual(int(detail[0]), waiting_case['order'].id)
        self.assertEqual(detail[12:], [
            '继续等待', '',
            timezone.localtime(
                MeetingResponseReminderReview.objects.get(
                    reminder__proposal_event=waiting_case['proposal'],
                ).reviewed_at,
            ).strftime('%Y-%m-%d %H:%M'),
            '窗口内仍需继续观察。', '1',
        ])

    def test_filtered_page_keeps_twenty_but_csv_exports_all_matching_rows(self):
        cases = [self.make_case() for _index in range(21)]
        self.send_cases()
        for index, case in enumerate(cases):
            self.review_case(
                case, self.staff, outcome='waiting', note='第 %d 条继续等待' % index,
                reviewed_at=self.now + timedelta(minutes=index),
            )

        default_page = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        )
        self.assertNotIn(
            'meeting-response-pending-review',
            default_page.content.decode('utf-8'),
        )

        page = self.client.get(
            reverse('operations_dashboard'),
            {'days': 30, 'review_status': 'waiting'},
        )
        html = page.content.decode('utf-8')
        panel_start = html.index('meeting-response-pending-review')
        panel = html[panel_start:html.index('按接收角色观察', panel_start)]
        linked_ids = [
            case['order'].id for case in cases
            if 'href="%s"' % reverse('order_detail', args=[case['order'].id]) in panel
        ]
        self.assertEqual(linked_ids, [case['order'].id for case in cases[:20]])
        self.assertIn('CSV 导出当前筛选全部 21 条', html)

        export = self.client.get(
            reverse('operations_dashboard_export'),
            {'days': 30, 'review_status': 'waiting'},
        )
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        header_index = next(
            index for index, row in enumerate(rows)
            if row and row[0] == '订单编号'
        )
        detail_rows = []
        for row in rows[header_index + 1:]:
            if not row:
                break
            detail_rows.append(row)
        self.assertEqual(len(detail_rows), 21)
        self.assertEqual(
            [int(row[0]) for row in detail_rows],
            [case['order'].id for case in cases],
        )
