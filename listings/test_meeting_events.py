# -*- coding: utf-8 -*-
"""交付安排事件链的写入、回滚和读取边界。"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .meeting_attendance import _build_round_stats
from .meeting_events import (
    first_response_seconds,
    proposal_response_pairs,
    record_meeting_event,
)
from .models import (
    CampusLocation,
    Category,
    Item,
    MeetingAppointment,
    MeetingAppointmentEvent,
    Order,
)
from .order_workflow import transition_order


class MeetingEventFlowTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='event-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='event-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='事件分类')
        self.location = CampusLocation.objects.create(name='事件原地点')
        self.other_location = CampusLocation.objects.create(name='事件新地点')
        self.item = Item.objects.create(
            title='事件商品', description='用于交付事件测试', price=Decimal('30.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        self.order = Order.objects.create(
            item=self.item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00',
            status='confirmed',
        )

    def login(self, user):
        self.client.force_login(user)

    def payload(self, start_at, *, location=None):
        start_at = start_at.replace(second=0, microsecond=0)
        end_at = start_at + timedelta(hours=1)
        local_timezone = timezone.get_current_timezone()
        return {
            'start_at': start_at.astimezone(local_timezone).strftime('%Y-%m-%dT%H:%M'),
            'end_at': end_at.astimezone(local_timezone).strftime('%Y-%m-%dT%H:%M'),
            'location': (location or self.location).id,
        }

    def propose(self, user, start_at, *, location=None):
        self.login(user)
        response = self.client.post(
            reverse('propose_meeting', args=[self.order.id]),
            self.payload(start_at, location=location),
        )
        self.assertRedirects(response, reverse('order_detail', args=[self.order.id]))
        return MeetingAppointment.objects.get(order=self.order)

    def test_initial_proposal_and_reschedule_keep_each_snapshot_in_order(self):
        initial_start = timezone.now() + timedelta(days=1)
        appointment = self.propose(self.buyer, initial_start)
        initial_snapshot = appointment.start_at

        replacement_start = initial_start + timedelta(hours=2)
        appointment = self.propose(
            self.seller, replacement_start, location=self.other_location,
        )

        events = list(appointment.events.order_by('occurred_at', 'pk'))
        self.assertEqual(
            [event.event_type for event in events], ['proposed', 'rescheduled'],
        )
        proposed, rescheduled = events
        self.assertEqual(proposed.previous_status, '')
        self.assertEqual(proposed.new_status, 'pending')
        self.assertEqual(proposed.proposed_by, self.buyer)
        self.assertEqual(proposed.proposed_start_at, initial_snapshot)
        self.assertEqual(proposed.location, self.location)
        self.assertEqual(rescheduled.previous_status, 'pending')
        self.assertEqual(rescheduled.proposed_by, self.seller)
        self.assertEqual(rescheduled.location, self.other_location)
        self.assertEqual(rescheduled.proposed_start_at, appointment.start_at)
        self.assertNotEqual(proposed.proposed_start_at, rescheduled.proposed_start_at)

    def test_acceptance_records_the_response_and_state_change(self):
        self.propose(self.buyer, timezone.now() + timedelta(days=1))

        self.login(self.seller)
        response = self.client.post(
            reverse('respond_meeting', args=[self.order.id, 'accept']),
        )
        self.assertRedirects(response, reverse('order_detail', args=[self.order.id]))

        accepted = MeetingAppointmentEvent.objects.get(
            order=self.order, event_type='accepted',
        )
        self.assertEqual(accepted.previous_status, 'pending')
        self.assertEqual(accepted.new_status, 'confirmed')
        self.assertEqual(accepted.actor, self.seller)
        self.assertEqual(accepted.responded_by, self.seller)
        self.assertIsNotNone(accepted.responded_at)

    def test_decline_records_the_reason_snapshot(self):
        self.propose(self.buyer, timezone.now() + timedelta(days=1))

        self.login(self.seller)
        response = self.client.post(
            reverse('respond_meeting', args=[self.order.id, 'decline']),
        )
        self.assertRedirects(response, reverse('order_detail', args=[self.order.id]))

        declined = MeetingAppointmentEvent.objects.get(
            order=self.order, event_type='declined',
        )
        self.assertEqual(declined.previous_status, 'pending')
        self.assertEqual(declined.new_status, 'declined')
        self.assertEqual(declined.responded_by, self.seller)
        self.assertEqual(declined.decline_reason, '对方暂未接受这次时间安排')

    def test_cancelling_the_order_cancels_the_appointment_and_records_an_event(self):
        self.propose(self.buyer, timezone.now() + timedelta(days=1))

        transition_order(
            order_id=self.order.id, actor=self.seller, target_status='cancelled',
        )

        appointment = MeetingAppointment.objects.get(order=self.order)
        cancelled = MeetingAppointmentEvent.objects.get(
            order=self.order, event_type='cancelled',
        )
        self.assertEqual(appointment.status, 'cancelled')
        self.assertEqual(cancelled.previous_status, 'pending')
        self.assertEqual(cancelled.new_status, 'cancelled')
        self.assertEqual(cancelled.actor, self.seller)

    def test_cancelling_without_an_appointment_does_not_create_an_event(self):
        transition_order(
            order_id=self.order.id, actor=self.seller, target_status='cancelled',
        )

        self.assertFalse(
            MeetingAppointmentEvent.objects.filter(order=self.order).exists(),
        )

    def test_notification_failure_rolls_back_the_appointment_and_event(self):
        self.login(self.buyer)
        with patch(
            'listings.views.create_notification', side_effect=RuntimeError('通知失败'),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse('propose_meeting', args=[self.order.id]),
                    self.payload(timezone.now() + timedelta(days=1)),
                )

        self.assertFalse(MeetingAppointment.objects.filter(order=self.order).exists())
        self.assertFalse(
            MeetingAppointmentEvent.objects.filter(order=self.order).exists(),
        )

    def test_a_saved_event_cannot_be_modified(self):
        appointment = self.propose(self.buyer, timezone.now() + timedelta(days=1))
        event = MeetingAppointmentEvent.objects.get(
            order=self.order, event_type='proposed',
        )
        original_start = event.proposed_start_at
        event.proposed_start_at = original_start + timedelta(hours=1)

        with self.assertRaises(ValidationError):
            event.save()

        event.refresh_from_db()
        self.assertEqual(event.proposed_start_at, original_start)

    def test_first_response_and_round_outcomes_stay_anchored_to_each_proposal(self):
        start_at = timezone.now() + timedelta(days=1)
        appointment = MeetingAppointment.objects.create(
            order=self.order, proposed_by=self.seller, location=self.location,
            start_at=start_at, end_at=start_at + timedelta(hours=1),
            status='pending',
        )
        proposed_at = timezone.now() - timedelta(hours=6)
        record_meeting_event(
            appointment=appointment, event_type='proposed', actor=self.seller,
            previous_status='', occurred_at=proposed_at,
        )
        appointment.status = 'confirmed'
        appointment.responded_by = self.buyer
        appointment.responded_at = proposed_at + timedelta(hours=2)
        appointment.save(update_fields=[
            'status', 'responded_by', 'responded_at', 'updated_at',
        ])
        record_meeting_event(
            appointment=appointment, event_type='accepted', actor=self.buyer,
            previous_status='pending', occurred_at=appointment.responded_at,
        )
        appointment.status = 'pending'
        appointment.responded_by = None
        appointment.responded_at = None
        appointment.save(update_fields=[
            'status', 'responded_by', 'responded_at', 'updated_at',
        ])
        record_meeting_event(
            appointment=appointment, event_type='rescheduled', actor=self.seller,
            previous_status='confirmed', occurred_at=proposed_at + timedelta(hours=3),
        )
        appointment.status = 'declined'
        appointment.responded_by = self.buyer
        appointment.responded_at = proposed_at + timedelta(hours=4)
        appointment.decline_reason = '再换一次'
        appointment.save(update_fields=[
            'status', 'responded_by', 'responded_at', 'decline_reason', 'updated_at',
        ])
        record_meeting_event(
            appointment=appointment, event_type='declined', actor=self.buyer,
            previous_status='pending', occurred_at=appointment.responded_at,
        )
        appointment.status = 'pending'
        appointment.responded_by = None
        appointment.responded_at = None
        appointment.decline_reason = ''
        appointment.save(update_fields=[
            'status', 'responded_by', 'responded_at', 'decline_reason', 'updated_at',
        ])
        record_meeting_event(
            appointment=appointment, event_type='rescheduled', actor=self.seller,
            previous_status='declined', occurred_at=proposed_at + timedelta(hours=5),
        )
        events = list(appointment.events.order_by('occurred_at', 'pk'))

        self.assertEqual(first_response_seconds(events), 7200.0)
        self.assertEqual(len(proposal_response_pairs(events)), 2)
        round_stats = _build_round_stats([events])
        self.assertEqual(round_stats['proposal_count'], 3)
        self.assertEqual(round_stats['accepted_count'], 1)
        self.assertEqual(round_stats['declined_count'], 1)
        self.assertEqual(round_stats['unanswered_count'], 1)
