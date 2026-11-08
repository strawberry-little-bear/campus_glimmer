# -*- coding: utf-8 -*-
"""交付安排待回应提醒的状态、去重与命令台账边界。"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import NotificationPreferenceForm
from .meeting_response_reminders import send_meeting_response_reminders
from .models import (
    CampusLocation,
    Category,
    Item,
    MeetingAppointment,
    MeetingAppointmentEvent,
    Notification,
    NotificationPreference,
    Order,
    TaskRun,
)


class MeetingResponseReminderTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.seller = User.objects.create_user(username='reminder-seller')
        self.buyer = User.objects.create_user(username='reminder-buyer')
        self.category = Category.objects.create(name='提醒测试分类')
        self.location = CampusLocation.objects.create(name='提醒测试地点')
        self.item = Item.objects.create(
            title='等待回应的商品',
            description='用于交付安排提醒测试',
            price=Decimal('20.00'),
            status='available',
            seller=self.seller,
            category=self.category,
            location=self.location,
        )
        self.order = Order.objects.create(
            item=self.item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price='20.00',
            status='confirmed',
        )
        self.start_at = self.now + timedelta(days=1)
        self.appointment = MeetingAppointment.objects.create(
            order=self.order,
            proposed_by=self.buyer,
            location=self.location,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=1),
        )

    def add_event(self, event_type='proposed', *, hours_ago=25, proposed_by='default'):
        if proposed_by == 'default':
            proposed_by = self.appointment.proposed_by
        previous_status = '' if event_type == 'proposed' else 'pending'
        if event_type in {'accepted', 'declined'}:
            new_status = 'confirmed' if event_type == 'accepted' else 'declined'
        elif event_type == 'cancelled':
            new_status = 'cancelled'
        else:
            new_status = 'pending'
        return MeetingAppointmentEvent.objects.create(
            order=self.order,
            appointment=self.appointment,
            event_type=event_type,
            occurred_at=self.now - timedelta(hours=hours_ago),
            actor=proposed_by,
            proposed_by=proposed_by,
            proposed_start_at=self.appointment.start_at,
            proposed_end_at=self.appointment.end_at,
            location=self.location,
            location_name=self.location.name,
            previous_status=previous_status,
            new_status=new_status,
        )

    def reschedule(self, *, hours_ago=25, proposed_by=None):
        proposed_by = proposed_by or self.seller
        self.appointment.proposed_by = proposed_by
        self.appointment.start_at = self.start_at + timedelta(hours=2)
        self.appointment.end_at = self.start_at + timedelta(hours=3)
        self.appointment.save(update_fields=[
            'proposed_by', 'start_at', 'end_at', 'updated_at',
        ])
        return self.add_event(
            'rescheduled', hours_ago=hours_ago, proposed_by=proposed_by,
        )

    def test_buyer_proposal_reminds_seller_after_default_window(self):
        proposal = self.add_event()

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['sent'], 1)
        notification = Notification.objects.get()
        self.assertEqual(notification.recipient, self.seller)
        self.assertEqual(notification.actor, self.buyer)
        self.assertEqual(notification.kind, 'meeting_response_pending')
        self.assertEqual(notification.order, self.order)
        self.assertEqual(notification.item, self.item)
        self.assertEqual(
            notification.target_url,
            reverse('order_detail', args=[self.order.id]),
        )
        self.assertEqual(notification.dedupe_key, f'meeting-response:{proposal.pk}')

    def test_seller_proposal_reminds_buyer(self):
        self.appointment.proposed_by = self.seller
        self.appointment.save(update_fields=['proposed_by', 'updated_at'])
        self.add_event(proposed_by=self.seller)

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['sent'], 1)
        self.assertEqual(Notification.objects.get().recipient, self.buyer)

    def test_recent_proposal_waits_for_the_threshold(self):
        self.add_event(hours_ago=23)

        result = send_meeting_response_reminders(now=self.now, response_hours=24)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_same_proposal_is_reminded_only_once(self):
        self.add_event()

        first = send_meeting_response_reminders(now=self.now)
        second = send_meeting_response_reminders(now=self.now)

        self.assertEqual(first['sent'], 1)
        self.assertEqual(second['reminded'], 1)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(Notification.objects.get().occurrence_count, 1)

    def test_accepted_or_declined_proposal_is_not_reminded(self):
        self.add_event()
        self.add_event('accepted')

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_cancelled_event_after_proposal_is_not_reminded(self):
        self.add_event()
        self.add_event('cancelled')

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_non_negotiation_order_and_appointment_states_are_skipped(self):
        self.add_event()
        self.order.status = 'completed'
        self.order.save(update_fields=['status', 'updated_at'])

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['candidates'], 0)
        self.assertEqual(result['skipped'], 0)
        self.assertFalse(Notification.objects.exists())

    def test_reschedule_creates_a_new_reminder_without_repeating_old_one(self):
        old_proposal = self.add_event(hours_ago=30)
        Notification.objects.create(
            recipient=self.seller,
            actor=self.buyer,
            order=self.order,
            item=self.item,
            kind='meeting_response_pending',
            title='旧提醒',
            message='旧提议已经提醒过',
            dedupe_key=f'meeting-response:{old_proposal.pk}',
        )
        new_proposal = self.reschedule(hours_ago=25)

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['sent'], 1)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertTrue(Notification.objects.filter(
            recipient=self.buyer,
            dedupe_key=f'meeting-response:{new_proposal.pk}',
        ).exists())
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.seller,
                dedupe_key=f'meeting-response:{old_proposal.pk}',
            ).count(),
            1,
        )

    def test_first_event_rescheduled_is_legacy_and_never_invented(self):
        self.add_event('rescheduled')

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_appointment_without_events_is_skipped(self):
        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_deleted_or_non_participant_proposer_is_skipped(self):
        self.add_event(proposed_by=None)

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_disabled_preference_blocks_notification(self):
        self.add_event()
        NotificationPreference.objects.create(
            user=self.seller,
            meeting_response_pending=False,
        )

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_dry_run_reports_due_reminder_without_writes(self):
        self.add_event()

        result = send_meeting_response_reminders(now=self.now, dry_run=True)

        self.assertEqual(result['sent'], 1)
        self.assertFalse(Notification.objects.exists())
        self.assertFalse(NotificationPreference.objects.exists())

    def test_mismatched_current_snapshot_is_not_trusted(self):
        self.add_event()
        self.appointment.start_at += timedelta(minutes=30)
        self.appointment.save(update_fields=['start_at', 'updated_at'])

        result = send_meeting_response_reminders(now=self.now)

        self.assertEqual(result['skipped'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_notification_kind_preference_and_form_stay_aligned(self):
        choice_values = {value for value, _ in Notification.KIND_CHOICES}
        preference_fields = {
            field.name
            for field in NotificationPreference._meta.get_fields()
            if getattr(field, 'concrete', False)
            and field.get_internal_type() == 'BooleanField'
            and field.name != 'quiet_hours_enabled'
        }

        self.assertIn('meeting_response_pending', choice_values)
        self.assertIn('meeting_response_pending', preference_fields)
        self.assertIn('meeting_response_pending', NotificationPreferenceForm.Meta.fields)


class MeetingResponseReminderCommandTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.seller = User.objects.create_user(username='command-reminder-seller')
        self.buyer = User.objects.create_user(username='command-reminder-buyer')
        category = Category.objects.create(name='命令提醒分类')
        location = CampusLocation.objects.create(name='命令提醒地点')
        item = Item.objects.create(
            title='命令提醒商品',
            description='用于命令台账测试',
            price=Decimal('15.00'),
            status='available',
            seller=self.seller,
            category=category,
            location=location,
        )
        order = Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=location,
            agreed_price='15.00',
            status='confirmed',
        )
        start_at = self.now + timedelta(days=1)
        appointment = MeetingAppointment.objects.create(
            order=order,
            proposed_by=self.buyer,
            location=location,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )
        MeetingAppointmentEvent.objects.create(
            order=order,
            appointment=appointment,
            event_type='proposed',
            occurred_at=self.now - timedelta(hours=25),
            actor=self.buyer,
            proposed_by=self.buyer,
            proposed_start_at=start_at,
            proposed_end_at=start_at + timedelta(hours=1),
            location=location,
            location_name=location.name,
            previous_status='',
            new_status='pending',
        )

    def test_command_records_success_and_comparable_metrics(self):
        call_command('send_meeting_response_reminders', stdout=StringIO())

        run = TaskRun.objects.get()
        self.assertEqual(run.name, 'send_meeting_response_reminders')
        self.assertEqual(run.status, 'succeeded')
        self.assertEqual(run.metrics['sent'], 1)
        self.assertEqual(run.metrics['skipped'], 0)
        self.assertEqual(run.metrics['reminded'], 0)
        self.assertEqual(run.metrics['failed'], 0)

    def test_dry_run_is_marked_and_keeps_notifications_empty(self):
        call_command(
            'send_meeting_response_reminders', '--dry-run', stdout=StringIO(),
        )

        run = TaskRun.objects.get()
        self.assertEqual(run.note, 'dry-run')
        self.assertEqual(run.metrics['sent'], 1)
        self.assertFalse(Notification.objects.exists())

    def test_command_failure_is_recorded_and_reraised(self):
        target = (
            'listings.management.commands.send_meeting_response_reminders.'
            'send_meeting_response_reminders'
        )
        with patch(target, side_effect=RuntimeError('提醒任务失败')):
            with self.assertRaises(RuntimeError):
                call_command('send_meeting_response_reminders', stdout=StringIO())

        run = TaskRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertIn('RuntimeError', run.note)

    def test_invalid_hours_fail_the_recorded_run(self):
        with self.assertRaises(ValueError):
            call_command(
                'send_meeting_response_reminders', hours=0, stdout=StringIO(),
            )

        run = TaskRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertIn('ValueError', run.note)
