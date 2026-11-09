# -*- coding: utf-8 -*-
"""交付预约异常处理动作的回归测试。"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation, Category, Item, MeetingAppointment, MeetingIncident,
    MeetingIncidentEvent, Notification, Order,
)


class MeetingIncidentEventFlowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='event-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='event-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='event-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='异常动作测试分类')
        self.location = CampusLocation.objects.create(name='异常动作测试地点')
        self.item = Item.objects.create(
            title='异常动作测试商品', description='用于测试预约异常处理动作。',
            price='25.00', category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        self.order = Order.objects.create(
            item=self.item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='25.00', status='completed',
        )
        now = timezone.now()
        self.appointment = MeetingAppointment.objects.create(
            order=self.order, proposed_by=self.seller, location=self.location,
            start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1),
            status='confirmed',
        )

    def make_incident(self, *, reported=False):
        incident = MeetingIncident.objects.create(
            appointment=self.appointment, reported_by=self.buyer,
            accused=self.seller, reason='no_show', detail='对方未到场，我等待了四十分钟。',
        )
        if reported:
            incident.record_event('reported', actor=self.buyer)
        return incident

    def event_rows(self, incident):
        return list(incident.events.values_list(
            'event_type', 'previous_status', 'new_status', 'actor_label', 'note',
        ))

    def test_submission_appends_the_report_event_and_both_parties_see_it(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertContains(response, '提交预约异常')

        response = self.client.post(
            reverse('report_meeting_incident', args=[self.order.id]),
            {'reason': 'late', 'detail': '对方迟到了四十分钟，现场等待后才离开。'},
        )

        self.assertRedirects(response, reverse('order_detail', args=[self.order.id]))
        incident = MeetingIncident.objects.get(appointment=self.appointment)
        self.assertEqual(self.event_rows(incident), [(
            'reported', '', 'open', self.buyer.username, '',
        )])
        for participant in (self.buyer, self.seller):
            self.client.force_login(participant)
            response = self.client.get(reverse('order_detail', args=[self.order.id]))
            self.assertContains(response, '预约异常')
            self.assertContains(response, '提交异常')

    def test_explicit_start_records_the_handler_and_open_to_reviewing_snapshot(self):
        incident = self.make_incident(reported=True)
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse('review_meeting_incident', args=[incident.id]),
            {'action': 'start'},
        )

        self.assertRedirects(
            response, reverse('review_meeting_incident', args=[incident.id]),
        )
        incident.refresh_from_db()
        self.assertEqual(incident.status, 'reviewing')
        self.assertEqual(incident.reviewer, self.staff)
        self.assertEqual(self.event_rows(incident), [
            ('reported', '', 'open', self.buyer.username, ''),
            ('review_started', 'open', 'reviewing', self.staff.username, ''),
        ])
        self.assertEqual(Notification.objects.filter(recipient=self.buyer).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.seller).count(), 1)
        response = self.client.get(
            reverse('review_meeting_incident', args=[incident.id]),
        )
        self.assertContains(response, '2 次动作')
        self.assertContains(response, self.staff.username)

    def test_direct_dismissal_adds_started_before_the_conclusion(self):
        incident = self.make_incident(reported=True)
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse('review_meeting_incident', args=[incident.id]),
            {'status': 'dismissed', 'resolution_note': '到场登记与提交说明不一致。'},
        )

        self.assertRedirects(response, reverse('governance_workbench'))
        incident.refresh_from_db()
        self.assertEqual(incident.status, 'dismissed')
        self.assertEqual(self.event_rows(incident), [
            ('reported', '', 'open', self.buyer.username, ''),
            ('review_started', 'open', 'reviewing', self.staff.username, ''),
            (
                'dismissed', 'reviewing', 'dismissed', self.staff.username,
                '到场登记与提交说明不一致。',
            ),
        ])
        for participant in (self.buyer, self.seller):
            self.client.force_login(participant)
            response = self.client.get(reverse('order_detail', args=[self.order.id]))
            self.assertContains(response, '开始处理')
            self.assertContains(response, '驳回异常')
            self.assertContains(response, '到场登记与提交说明不一致。')

    def test_start_then_resolution_keeps_the_complete_chain(self):
        incident = self.make_incident(reported=True)
        self.client.force_login(self.staff)
        review_url = reverse('review_meeting_incident', args=[incident.id])
        self.client.post(review_url, {'action': 'start'})

        response = self.client.post(
            review_url,
            {'status': 'resolved', 'resolution_note': '已核对双方到场登记。'},
        )

        self.assertRedirects(response, reverse('governance_workbench'))
        self.assertEqual(self.event_rows(incident), [
            ('reported', '', 'open', self.buyer.username, ''),
            ('review_started', 'open', 'reviewing', self.staff.username, ''),
            (
                'resolved', 'reviewing', 'resolved', self.staff.username,
                '已核对双方到场登记。',
            ),
        ])

    def test_a_note_update_records_its_snapshot_without_changing_status(self):
        incident = self.make_incident()

        event = incident.record_event(
            'note_updated', actor=self.staff, note='等待补充到场证明。',
        )

        self.assertEqual(event.previous_status, 'open')
        self.assertEqual(event.new_status, 'open')
        self.assertEqual(event.actor_label, self.staff.username)
        self.assertEqual(event.note, '等待补充到场证明。')

    def test_an_event_cannot_be_saved_again_and_its_actor_label_survives_deletion(self):
        incident = self.make_incident()
        event = incident.record_event('reported', actor=self.staff)
        event.note = '试图改写历史'
        with self.assertRaises(ValidationError):
            event.save()
        self.assertEqual(
            MeetingIncidentEvent.objects.get(pk=event.pk).note,
            '',
        )

        self.staff.delete()
        event.refresh_from_db()
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_label, 'event-staff')

    def test_legacy_incident_without_events_is_not_given_fabricated_history(self):
        incident = self.make_incident()
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse('review_meeting_incident', args=[incident.id]),
        )
        self.assertContains(response, '0 次动作')
        self.assertContains(response, '系统不会为之前缺失的动作补造历史')

        response = self.client.get(
            reverse('governance_workbench'), {'kind': 'incident'},
        )
        self.assertContains(response, '旧异常未留存处理动作')

        for participant in (self.buyer, self.seller):
            self.client.force_login(participant)
            response = self.client.get(reverse('order_detail', args=[self.order.id]))
            self.assertContains(response, '此前缺失的动作不会补造')

    def test_governance_workbench_exposes_the_compact_action_chain(self):
        incident = self.make_incident(reported=True)
        incident.record_event('review_started', actor=self.staff)
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse('governance_workbench'), {'kind': 'incident'},
        )

        self.assertEqual(len(response.context['cases']), 1)
        self.assertEqual(
            [event['event_type'] for event in response.context['cases'][0]['incident_events']],
            ['reported', 'review_started'],
        )
        self.assertContains(response, '预约异常处理链')
        self.assertContains(response, '提交异常')
        self.assertContains(response, '开始处理')
