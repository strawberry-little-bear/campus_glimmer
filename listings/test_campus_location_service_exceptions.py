from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .campus_location_service_hours import (
    evaluate_location_service_window,
    evaluate_service_windows,
    format_service_exceptions,
    get_active_service_exceptions,
)
from .meeting_scheduling import recommend_meeting_locations, recommend_meeting_times
from .models import (
    CampusLocation,
    CampusLocationServiceException,
    CampusLocationServiceExceptionWindow,
    CampusLocationServiceWindow,
    Category,
    Item,
    Order,
)


class CampusLocationServiceExceptionModelTests(TestCase):
    def setUp(self):
        self.location = CampusLocation.objects.create(name='单日例外地点')

    def test_replacement_window_must_end_after_start(self):
        exception = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.REPLACEMENT,
            reason='考试周调整',
        )
        invalid = CampusLocationServiceExceptionWindow(
            service_exception=exception, start_time=time(14), end_time=time(14),
        )
        with self.assertRaises(ValidationError) as context:
            invalid.clean()
        self.assertIn('end_time', context.exception.error_dict)

    def test_only_one_active_exception_is_allowed_for_a_date(self):
        CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.CLOSED,
            reason=' historical configuration',
            is_active=False,
        )
        CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.REPLACEMENT,
            reason='考试周调整',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusLocationServiceException.objects.create(
                location=self.location,
                effective_date=date(2026, 10, 5),
                exception_type=CampusLocationServiceException.CLOSED,
                reason='重复配置',
            )

    def test_duplicate_replacement_start_time_is_rejected(self):
        exception = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.REPLACEMENT,
            reason='考试周调整',
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception, start_time=time(9), end_time=time(12),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusLocationServiceExceptionWindow.objects.create(
                service_exception=exception, start_time=time(9), end_time=time(15),
            )

    def test_exception_summary_contains_reason_and_replacement_windows(self):
        exception = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.REPLACEMENT,
            reason='考试周调整',
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception, start_time=time(9), end_time=time(12),
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception, start_time=time(12), end_time=time(14),
        )
        self.assertEqual(
            format_service_exceptions((exception,)),
            '2026-10-05替换为 09:00–12:00、12:00–14:00（考试周调整）',
        )


class CampusLocationServiceExceptionEvaluationTests(TestCase):
    def setUp(self):
        self.location = CampusLocation.objects.create(name='例外判断地点')
        self.weekly = [
            CampusLocationServiceWindow(
                location=self.location, weekday=1,
                start_time=time(9), end_time=time(18),
            ),
            CampusLocationServiceWindow(
                location=self.location, weekday=7,
                start_time=time(22), end_time=time(2),
            ),
        ]

    def local(self, value):
        return timezone.make_aware(value)

    def create_exception(self, exception_type, reason='假期闭馆', is_active=True):
        return CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=exception_type,
            reason=reason,
            is_active=is_active,
        )

    def test_closed_exception_overrides_weekly_window(self):
        exception = self.create_exception(CampusLocationServiceException.CLOSED)
        evaluation = evaluate_service_windows(
            self.weekly,
            start_at=self.local(datetime(2026, 10, 5, 10)),
            end_at=self.local(datetime(2026, 10, 5, 11)),
            exceptions=(exception,),
        )
        self.assertEqual(evaluation.state, 'closed')
        self.assertIn('2026-10-05临时关闭（假期闭馆）', evaluation.summary)
        self.assertEqual(evaluation.exception_reasons, ('2026-10-05临时关闭：假期闭馆',))

    def test_closed_exception_blocks_previous_cross_midnight_window(self):
        exception = self.create_exception(CampusLocationServiceException.CLOSED)
        evaluation = evaluate_service_windows(
            self.weekly,
            start_at=self.local(datetime(2026, 10, 5, 0, 30)),
            end_at=self.local(datetime(2026, 10, 5, 1)),
            exceptions=(exception,),
        )
        self.assertEqual(evaluation.state, 'closed')

    def test_replacement_replaces_weekly_windows_and_merges_adjacent_intervals(self):
        exception = self.create_exception(
            CampusLocationServiceException.REPLACEMENT, reason='考试周调整',
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception, start_time=time(9), end_time=time(12),
        )
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=exception, start_time=time(12), end_time=time(14),
        )
        covered = evaluate_service_windows(
            self.weekly,
            start_at=self.local(datetime(2026, 10, 5, 10)),
            end_at=self.local(datetime(2026, 10, 5, 13)),
            exceptions=(exception,),
        )
        outside = evaluate_service_windows(
            self.weekly,
            start_at=self.local(datetime(2026, 10, 5, 15)),
            end_at=self.local(datetime(2026, 10, 5, 16)),
            exceptions=(exception,),
        )
        self.assertEqual(covered.state, 'open')
        self.assertEqual(outside.state, 'closed')

    def test_inactive_exception_restores_weekly_rule(self):
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        self.create_exception(
            CampusLocationServiceException.CLOSED, is_active=False,
        )
        evaluation = evaluate_location_service_window(
            self.location,
            start_at=self.local(datetime(2026, 10, 5, 10)),
        )
        self.assertEqual(evaluation.state, 'open')
        self.assertFalse(evaluation.exceptions)

    def test_exception_does_not_turn_missing_weekly_schedule_into_registered(self):
        self.create_exception(CampusLocationServiceException.CLOSED)
        evaluation = evaluate_location_service_window(
            self.location,
            start_at=self.local(datetime(2026, 10, 5, 10)),
        )
        self.assertEqual(evaluation.state, 'unregistered')
        self.assertFalse(evaluation.exceptions)

    def test_out_of_range_exception_is_not_shown_in_summary(self):
        exception = self.create_exception(
            CampusLocationServiceException.CLOSED,
            reason='另一个日期',
        )
        exception.effective_date = date(2026, 10, 6)
        evaluation = evaluate_service_windows(
            self.weekly,
            start_at=self.local(datetime(2026, 10, 5, 10)),
            exceptions=(exception,),
        )
        self.assertEqual(evaluation.state, 'open')
        self.assertNotIn('另一个日期', evaluation.summary)
        self.assertFalse(evaluation.exceptions)


class MeetingServiceExceptionRecommendationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='exception-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='exception-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='单日例外分类')
        self.location = CampusLocation.objects.create(name='单日例外交付点', is_public=True)
        self.other_location = CampusLocation.objects.create(name='常规开放交付点', is_public=True)
        self.order = Order.objects.create(
            item=Item.objects.create(
                title='单日例外商品', description='用于测试例外推荐', price='20.00',
                category=self.category, location=self.location,
                condition='良好', seller=self.seller,
            ),
            buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='20.00', status='confirmed',
        )
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        CampusLocationServiceWindow.objects.create(
            location=self.other_location, weekday=1,
            start_time=time(9), end_time=time(18),
        )
        self.exception = CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=date(2026, 10, 5),
            exception_type=CampusLocationServiceException.CLOSED,
            reason='假期闭馆',
        )

    def test_location_recommendation_explains_closed_exception(self):
        recommendations = recommend_meeting_locations(
            order=self.order,
            target_start_at=timezone.make_aware(datetime(2026, 10, 5, 10)),
            target_end_at=timezone.make_aware(datetime(2026, 10, 5, 11)),
            limit=5,
        )
        self.assertTrue(any(item.service_state == 'open' for item in recommendations))
        closed = next(
            item for item in recommendations if item.location == self.location
        )
        self.assertTrue(any('临时关闭' in reason for reason in closed.reasons))
        self.assertIn('假期闭馆', closed.service_hours_text)

    def test_time_recommendation_prefers_replacement_window(self):
        self.exception.exception_type = CampusLocationServiceException.REPLACEMENT
        self.exception.reason = '考试周延长开放'
        self.exception.save()
        CampusLocationServiceExceptionWindow.objects.create(
            service_exception=self.exception,
            start_time=time(17), end_time=time(18),
        )
        now = timezone.make_aware(datetime(2026, 10, 5, 8))
        recommendations = recommend_meeting_times(
            order=self.order, location=self.location, now=now, days=1, limit=12,
        )
        self.assertEqual(timezone.localtime(recommendations[0].start_at).hour, 17)
        self.assertTrue(any(
            '当天例外开放时段' in reason for reason in recommendations[0].reasons
        ))

    def test_prefetched_exception_lookup_keeps_active_rows(self):
        active = get_active_service_exceptions(self.location)
        self.assertEqual(active, (self.exception,))


class MeetingServiceExceptionSubmissionTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='submit-exception-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='submit-exception-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='提交例外分类')
        self.location = CampusLocation.objects.create(name='提交例外地点', is_public=True)
        self.order = Order.objects.create(
            item=Item.objects.create(
                title='提交例外商品', description='用于测试单日例外提交', price='25.00',
                category=self.category, location=self.location,
                condition='良好', seller=self.seller,
            ),
            buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='25.00', status='confirmed',
        )
        CampusLocationServiceWindow.objects.create(
            location=self.location, weekday=1,
            start_time=time(9), end_time=time(18),
        )

    def test_closed_exception_submission_is_saved_with_reason_reminder(self):
        local_now = timezone.localtime(timezone.now())
        candidate_date = local_now.date() + timedelta(days=1)
        while candidate_date.isoweekday() != 1:
            candidate_date += timedelta(days=1)
        CampusLocationServiceException.objects.create(
            location=self.location,
            effective_date=candidate_date,
            exception_type=CampusLocationServiceException.CLOSED,
            reason='假期闭馆',
        )
        start_at = datetime.combine(candidate_date, time(10))
        self.client.login(username='submit-exception-seller', password='safe-password-123')

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
        self.assertEqual(self.order.appointment.location, self.location)
        self.assertContains(response, '当天开放安排有单日例外')
        self.assertContains(response, '假期闭馆')
