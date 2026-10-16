from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campus_location_service_hours import evaluate_location_service_window, evaluate_service_windows, format_service_windows
from .meeting_scheduling import recommend_meeting_locations, recommend_meeting_times
from .models import CampusLocation, CampusLocationServiceWindow, Category, Item, Order


class CampusLocationServiceWindowModelTests(TestCase):
    def setUp(self):
        self.location = CampusLocation.objects.create(name='服务时段地点', is_public=True)

    def test_equal_start_and_end_is_rejected(self):
        window = CampusLocationServiceWindow(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(9),
        )
        with self.assertRaises(ValidationError) as context:
            window.clean()
        self.assertIn('end_time', context.exception.error_dict)

    def test_duplicate_location_weekday_and_start_is_rejected(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(12),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusLocationServiceWindow.objects.create(
                location=self.location, weekday=1,
                start_time=time(9), end_time=time(15),
            )

    def test_summary_groups_by_weekday_and_marks_cross_midnight(self):
        windows = [
            CampusLocationServiceWindow(
                location=self.location, weekday=1,
                start_time=time(9), end_time=time(12),
            ),
            CampusLocationServiceWindow(
                location=self.location, weekday=1,
                start_time=time(14), end_time=time(18),
            ),
            CampusLocationServiceWindow(
                location=self.location, weekday=7,
                start_time=time(22), end_time=time(2),
            ),
        ]
        self.assertEqual(
            format_service_windows(windows),
            '周一 09:00–12:00、14:00–18:00；周日 22:00–次日 02:00',
        )


class CampusLocationServiceWindowEvaluationTests(TestCase):
    def setUp(self):
        self.location = CampusLocation.objects.create(name='时段判断地点')
        self.windows = [
            CampusLocationServiceWindow(
                location=self.location, weekday=1,
                start_time=time(9), end_time=time(12),
            ),
            CampusLocationServiceWindow(
                location=self.location, weekday=1,
                start_time=time(12), end_time=time(15),
            ),
            CampusLocationServiceWindow(
                location=self.location, weekday=7,
                start_time=time(22), end_time=time(2),
            ),
        ]

    def local(self, value):
        return timezone.make_aware(value)

    def test_normal_and_adjacent_windows_cover_full_interval(self):
        evaluation = evaluate_service_windows(
            self.windows,
            start_at=self.local(datetime(2026, 10, 5, 10)),
            end_at=self.local(datetime(2026, 10, 5, 14)),
        )
        self.assertTrue(evaluation.is_open)
        self.assertEqual(evaluation.state, 'open')

    def test_half_covered_interval_is_closed(self):
        evaluation = evaluate_service_windows(
            self.windows,
            start_at=self.local(datetime(2026, 10, 5, 14)),
            end_at=self.local(datetime(2026, 10, 5, 16)),
        )
        self.assertEqual(evaluation.state, 'closed')

    def test_cross_midnight_window_covers_next_morning(self):
        evaluation = evaluate_service_windows(
            self.windows,
            start_at=self.local(datetime(2026, 10, 5, 0, 30)),
            end_at=self.local(datetime(2026, 10, 5, 1)),
        )
        self.assertEqual(evaluation.state, 'open')

    def test_moment_uses_half_open_boundary(self):
        start_at = self.local(datetime(2026, 10, 5, 12))
        opening = evaluate_service_windows(self.windows, start_at=start_at)
        before_opening = evaluate_service_windows(
            self.windows, start_at=start_at - timedelta(minutes=1),
        )
        self.assertEqual(opening.state, 'open')
        self.assertEqual(before_opening.state, 'open')

    def test_inactive_and_missing_windows_are_not_treated_as_closed(self):
        window = CampusLocationServiceWindow(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(12), is_active=False,
        )
        evaluation = evaluate_location_service_window(
            self.location, start_at=self.local(datetime(2026, 10, 5, 10)),
        )
        self.assertEqual(evaluation.state, 'unregistered')
        self.assertFalse(evaluation.is_registered)


class MeetingServiceWindowRecommendationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='service-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='service-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='开放时段分类')
        self.location = CampusLocation.objects.create(name='开放时段图书馆', is_public=True)
        self.other_location = CampusLocation.objects.create(name='未登记地点', is_public=True)
        self.order = Order.objects.create(
            item=Item.objects.create(
                title='开放时段商品', description='用于测试地点时段', price='20.00',
                category=self.category, location=self.location,
                condition='良好', seller=self.seller,
            ),
            buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='20.00', status='confirmed',
        )

    def test_location_recommendation_explains_registered_hours_without_target_time(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        recommendations = recommend_meeting_locations(order=self.order)
        registered = next(item for item in recommendations if item.location == self.location)
        unregistered = next(item for item in recommendations if item.location == self.other_location)

        self.assertEqual(registered.service_state, 'registered')
        self.assertEqual(registered.service_hours_text, '周一 09:00–18:00')
        self.assertTrue(any('已登记每周开放时段' in reason for reason in registered.reasons))
        self.assertEqual(unregistered.service_state, 'unregistered')
        self.assertEqual(unregistered.score, 10)
        self.assertTrue(any('未登记开放时段' in reason for reason in unregistered.reasons))

    def test_location_recommendation_marks_outside_target_window(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(12),
        )
        recommendations = recommend_meeting_locations(
            order=self.order,
            target_start_at=timezone.make_aware(datetime(2026, 10, 5, 14)),
            target_end_at=timezone.make_aware(datetime(2026, 10, 5, 15)),
        )
        recommendation = recommendations[0]
        self.assertEqual(recommendation.service_state, 'closed')
        self.assertTrue(any('不在登记开放时段' in reason for reason in recommendation.reasons))

    def test_time_recommendation_prefers_registered_open_window(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(17), end_time=time(18),
        )
        now = timezone.make_aware(datetime(2026, 10, 5, 8))
        recommendations = recommend_meeting_times(
            order=self.order, location=self.location, now=now, days=1, limit=12,
        )
        self.assertTrue(recommendations)
        self.assertEqual(timezone.localtime(recommendations[0].start_at).hour, 17)
        self.assertTrue(any('落在交付地点登记开放时段内' in reason for reason in recommendations[0].reasons))

    def test_time_recommendation_does_not_hide_closed_slots(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(17), end_time=time(18),
        )
        now = timezone.make_aware(datetime(2026, 10, 5, 8))
        recommendations = recommend_meeting_times(
            order=self.order, location=self.location, now=now, days=1, limit=20,
        )
        closed = [
            recommendation for recommendation in recommendations
            if timezone.localtime(recommendation.start_at).hour != 17
        ]
        self.assertTrue(closed)
        self.assertTrue(all(
            any('不在交付地点登记开放时段内' in reason for reason in recommendation.reasons)
            for recommendation in closed
        ))


class MeetingServiceWindowSubmissionTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='submit-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='submit-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='提交时段分类')
        self.location = CampusLocation.objects.create(name='提交时段地点', is_public=True)
        self.order = Order.objects.create(
            item=Item.objects.create(
                title='提交时段商品', description='用于测试提交提醒', price='25.00',
                category=self.category, location=self.location,
                condition='良好', seller=self.seller,
            ),
            buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='25.00', status='confirmed',
        )

    def test_outside_window_submission_is_saved_with_reminder(self):
        local_now = timezone.localtime(timezone.now())
        candidate_date = local_now.date() + timedelta(days=1)
        while candidate_date.isoweekday() != 1:
            candidate_date += timedelta(days=1)
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(10),
        )
        start_at = datetime.combine(candidate_date, time(20))
        self.client.login(username='submit-seller', password='safe-password-123')

        response = self.client.post(
            reverse('propose_meeting', args=[self.order.id]),
            {
                'start_at': start_at.strftime('%Y-%m-%dT%H:%M'),
                'end_at': (start_at + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
                'location': self.location.id,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        appointment = self.order.appointment
        self.assertEqual(appointment.location, self.location)
        self.assertEqual(timezone.localtime(appointment.start_at).hour, 20)
        self.assertContains(response, '所选时间不在「提交时段地点」登记开放时段')
        self.assertContains(response, '当前安排不在登记开放时段内')
