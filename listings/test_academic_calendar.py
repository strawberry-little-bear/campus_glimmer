from datetime import date, datetime, timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .academic_calendar import (
    academic_phase_context,
    academic_phase_summary_line,
    build_academic_calendar,
    ensure_default_phases,
    refresh_academic_term_snapshots,
    resolve_academic_phase,
)
from .models import (
    AcademicPhase,
    AcademicTerm,
    AcademicTermSnapshot,
    CampusLocation,
    Category,
    DemandPost,
    Item,
    Notification,
    NotificationPreference,
    Order,
    SavedSearch,
    SavedSearchMatch,
    SearchQuery,
)
from .opportunity_digest import send_opportunity_digest
from .saved_search_digest import send_saved_search_digest


class AcademicCalendarTests(TestCase):
    """Covers the term calendar, its phase rules and the derived statistics."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='calendar-owner', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='calendar-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='日历测试分类')
        self.location = CampusLocation.objects.create(name='日历测试地点')
        # A term that always contains "today", so phase boundaries are stable.
        self.today = timezone.localdate()
        self.term = AcademicTerm.objects.create(
            name='秋季学期（测试）',
            slug='autumn-test',
            kind='autumn',
            starts_on=self.today - timedelta(days=20),
            ends_on=self.today + timedelta(days=100),
        )
        ensure_default_phases(self.term)

    def make_item(self, *, title, created_at=None, price='20.00'):
        item = Item.objects.create(
            seller=self.user,
            title=title,
            description='用于学期日历测试',
            category=self.category,
            location=self.location,
            condition='八成新',
            price=price,
        )
        if created_at is not None:
            Item.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def make_order(self, *, created_at, status='completed'):
        item = self.make_item(title='日历测试预约商品', created_at=created_at)
        return Order.objects.create(
            item=item, buyer=self.buyer, seller=self.user,
            meeting_location=self.location, agreed_price=item.price,
            status=status, created_at=created_at,
        )

    def make_search(self, *, created_at, result_count=0):
        return SearchQuery.objects.create(
            user=self.user,
            query='日历测试关键词',
            result_count=result_count,
            created_at=created_at,
        )

    def test_default_phase_plan_is_idempotent(self):
        self.assertEqual(self.term.phases.count(), 4)

        created_again = ensure_default_phases(self.term)

        self.assertEqual(created_again, 0)
        self.assertEqual(self.term.phases.count(), 4)

    def test_ensure_default_phases_keeps_custom_rows(self):
        AcademicPhase.objects.filter(term=self.term).delete()
        AcademicPhase.objects.create(
            term=self.term, phase='holiday', start_offset=14,
            end_offset=20, note='自定义阶段',
        )

        created = ensure_default_phases(self.term)

        # offset 14 already exists, so only the other three are filled in.
        self.assertEqual(created, 3)
        self.assertTrue(
            AcademicPhase.objects.filter(term=self.term, phase='holiday').exists()
        )

    def test_resolve_academic_phase_outside_any_term(self):
        term, phase = resolve_academic_phase(self.today + timedelta(days=400))

        self.assertIsNone(term)
        self.assertIsNone(phase)

    def test_resolve_academic_phase_inside_configured_phase(self):
        term, phase = resolve_academic_phase(self.today)

        self.assertEqual(term, self.term)
        self.assertEqual(phase.phase, 'regular')

    def test_resolve_academic_phase_when_term_has_no_phase_for_day(self):
        AcademicPhase.objects.filter(term=self.term).delete()
        AcademicPhase.objects.create(
            term=self.term, phase='regular', start_offset=0, end_offset=5,
        )

        term, phase = resolve_academic_phase(self.today)

        self.assertEqual(term, self.term)
        self.assertIsNone(phase)

    def test_phase_context_reports_progress_and_days_left(self):
        context = academic_phase_context(self.today)

        self.assertTrue(context['has_term'])
        self.assertEqual(context['term_name'], self.term.name)
        self.assertEqual(context['phase_label'], '常规教学周')
        self.assertEqual(context['phase_note'], '常规教学周，以日常兴趣和小件为主')
        self.assertGreaterEqual(context['progress_percent'], 0)
        self.assertLessEqual(context['progress_percent'], 100)
        self.assertIsInstance(context['days_left'], int)

    def test_phase_context_without_term_is_explicit(self):
        AcademicTerm.objects.all().delete()

        context = academic_phase_context(self.today)

        self.assertFalse(context['has_term'])
        self.assertEqual(context['term_name'], '')
        self.assertEqual(context['phase_key'], '')
        self.assertIsNone(context['days_left'])

    def test_summary_line_mentions_phase_note(self):
        line = academic_phase_summary_line(day=self.today)

        self.assertIn(self.term.name, line)
        self.assertIn('常规教学周', line)
        self.assertIn('以日常兴趣和小件为主', line)
        self.assertTrue(line.endswith('。'))

    def test_summary_line_is_empty_without_term(self):
        AcademicTerm.objects.all().delete()

        self.assertEqual(academic_phase_summary_line(day=self.today), '')

    def test_build_academic_calendar_groups_phases_per_term(self):
        self.make_item(title='日历测试供给', created_at=timezone.now())
        self.make_order(created_at=timezone.now())
        self.make_search(created_at=timezone.now(), result_count=0)

        calendar = build_academic_calendar(day=self.today, limit_terms=3)

        self.assertTrue(calendar['has_terms'])
        self.assertEqual(len(calendar['terms']), 1)
        term_row = calendar['terms'][0]
        self.assertEqual(term_row['term_name'], self.term.name)
        self.assertTrue(term_row['is_current'])
        self.assertEqual(len(term_row['phases']), 4)
        current = [row for row in term_row['phases'] if row['is_current']]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['new_items'], 2)
        self.assertEqual(current[0]['completed_orders'], 1)
        self.assertEqual(current[0]['zero_result_searches'], 1)
        # The first phase of a term has nothing to compare against.
        self.assertEqual(term_row['phases'][0]['supply_trend'], 'baseline')
        self.assertEqual(term_row['phases'][0]['previous_phase_label'], '')

    def test_supply_trend_compares_two_populated_phases(self):
        registration_start = self.term.starts_on
        regular_start = registration_start + timedelta(days=14)
        exam_start = registration_start + timedelta(days=98)
        self.make_item(title='开学周教材', created_at=timezone.make_aware(
            datetime.combine(registration_start, datetime.min.time()),
        ))
        self.make_item(title='常规周小件一', created_at=timezone.make_aware(
            datetime.combine(regular_start, datetime.min.time()),
        ))
        self.make_item(title='常规周小件二', created_at=timezone.make_aware(
            datetime.combine(regular_start, datetime.min.time()),
        ))
        self.make_item(title='考试周资料一', created_at=timezone.make_aware(
            datetime.combine(exam_start, datetime.min.time()),
        ))

        calendar = build_academic_calendar(day=self.today, limit_terms=1)

        phases = {row['phase_key']: row for row in calendar['terms'][0]['phases']}
        self.assertEqual(phases['regular']['supply_trend'], 'rising')
        self.assertEqual(phases['regular']['supply_trend_label'], '供给上升')
        self.assertEqual(phases['exam']['supply_trend'], 'falling')
        self.assertEqual(phases['exam']['supply_trend_label'], '供给回落')
        self.assertEqual(phases['exam']['delta_new_items'], -1)
        self.assertEqual(phases['exam']['previous_new_items'], 2)

    def test_build_academic_calendar_without_terms(self):
        AcademicTerm.objects.all().delete()

        calendar = build_academic_calendar(day=self.today)

        self.assertFalse(calendar['has_terms'])
        self.assertEqual(calendar['terms'], [])
        self.assertIsNone(calendar['current'])

    def test_build_academic_calendar_compares_with_previous_phase(self):
        registration_start = self.term.starts_on
        exam_start = registration_start + timedelta(days=98)
        self.make_item(title='开学周教材', created_at=timezone.make_aware(
            datetime.combine(registration_start, datetime.min.time()),
        ))
        self.make_item(title='考试周资料一', created_at=timezone.make_aware(
            datetime.combine(exam_start, datetime.min.time()),
        ))
        self.make_item(title='考试周资料二', created_at=timezone.make_aware(
            datetime.combine(exam_start, datetime.min.time()),
        ))

        calendar = build_academic_calendar(day=self.today, limit_terms=1)

        phases = {row['phase_key']: row for row in calendar['terms'][0]['phases']}
        self.assertEqual(phases['registration']['new_items'], 1)
        self.assertEqual(phases['exam']['new_items'], 2)
        self.assertEqual(phases['exam']['delta_new_items'], 2)
        self.assertEqual(phases['exam']['previous_phase_label'], '常规教学周')
        # The regular phase stayed empty, so the exam phase has no baseline.
        self.assertEqual(phases['regular']['new_items'], 0)
        self.assertEqual(phases['exam']['supply_trend'], 'baseline')

    def test_refresh_snapshots_writes_one_row_per_phase(self):
        self.make_item(title='快照测试供给', created_at=timezone.now())

        updated = refresh_academic_term_snapshots()

        self.assertEqual(updated, 4)
        snapshots = AcademicTermSnapshot.objects.filter(term=self.term)
        self.assertEqual(snapshots.count(), 4)
        current = snapshots.get(phase_key='regular')
        self.assertEqual(current.new_items, 1)
        self.assertEqual(current.phase_label, '常规教学周')
        self.assertEqual(current.day_count, (current.end_date - current.start_date).days + 1)

        # A second refresh updates in place instead of adding rows.
        refresh_academic_term_snapshots()
        self.assertEqual(AcademicTermSnapshot.objects.filter(term=self.term).count(), 4)

    def test_sync_command_reports_phases_and_snapshots(self):
        AcademicPhase.objects.filter(term=self.term).delete()
        self.make_item(title='命令测试供给', created_at=timezone.now())
        output = StringIO()

        call_command('sync_academic_calendar', stdout=output)

        self.assertIn('处理学期 1 个', output.getvalue())
        self.assertIn('新增阶段 4 条', output.getvalue())
        self.assertIn('刷新阶段快照 4 条', output.getvalue())
        self.assertEqual(AcademicTermSnapshot.objects.count(), 4)

    def test_sync_command_can_target_one_term_and_one_step(self):
        other = AcademicTerm.objects.create(
            name='春季学期（测试）', slug='spring-test', kind='spring',
            starts_on=self.today - timedelta(days=200),
            ends_on=self.today - timedelta(days=90),
        )
        AcademicPhase.objects.all().delete()
        output = StringIO()

        call_command(
            'sync_academic_calendar', '--term-slug', 'spring-test',
            '--ensure-phases', stdout=output,
        )

        self.assertIn('处理学期 1 个', output.getvalue())
        self.assertEqual(other.phases.count(), 4)
        self.assertEqual(self.term.phases.count(), 0)
        self.assertFalse(AcademicTermSnapshot.objects.exists())

    def test_operations_dashboard_renders_academic_calendar_panel(self):
        staff = User.objects.create_user(
            username='calendar-staff', password='safe-password-123',
        )
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.login(username='calendar-staff', password='safe-password-123')
        self.make_item(title='看板测试供给', created_at=timezone.now())

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '学期节律与交易高峰')
        self.assertContains(response, '常规教学周')
        self.assertContains(response, self.term.name)
        self.assertContains(response, '当前学期')

    def test_operations_dashboard_explains_missing_calendar(self):
        AcademicTerm.objects.all().delete()
        staff = User.objects.create_user(
            username='calendar-staff-empty', password='safe-password-123',
        )
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.login(username='calendar-staff-empty', password='safe-password-123')

        response = self.client.get(reverse('operations_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '还没有配置学期日历')
        self.assertNotContains(response, '当前学期')

    def test_sync_command_warns_without_active_terms(self):
        AcademicTerm.objects.all().delete()
        output = StringIO()

        call_command('sync_academic_calendar', stdout=output)

        self.assertIn('没有找到启用中的学期', output.getvalue())

    def test_opportunity_digest_carries_phase_context(self):
        demand = DemandPost.objects.create(
            requester=self.buyer,
            title='求购日历测试教材',
            description='希望找到阶段相关的教材',
            category=self.category,
            location=self.location,
            max_price='50.00',
        )
        item = self.make_item(title='日历测试教材')
        self.assertIsNotNone(demand)
        self.assertIsNotNone(item)

        result = send_opportunity_digest(digest_date=self.today)

        self.assertEqual(result['sent'], 1)
        notification = Notification.objects.get(kind='opportunity_digest')
        self.assertIn('互助机会页面查看匹配依据', notification.message)
        self.assertIn('常规教学周', notification.message)

    def test_saved_search_digest_carries_phase_context(self):
        saved_search = SavedSearch.objects.create(
            user=self.user, name='日历测试关注', query='日历',
            category=self.category, location=self.location,
            notify_frequency='daily', max_matches_per_notice=3,
        )
        NotificationPreference.objects.get_or_create(user=self.user)
        item = self.make_item(title='日历测试关注命中商品')
        SavedSearchMatch.objects.create(saved_search=saved_search, item=item)

        result = send_saved_search_digest(today=self.today)

        self.assertEqual(result['sent'], 1)
        notification = Notification.objects.get(kind='saved_search_digest')
        self.assertIn('打开关注搜索查看详情', notification.message)
        self.assertIn('常规教学周', notification.message)
        # The phase sentence is appended after the call to action, not before.
        self.assertLess(
            notification.message.index('常规教学周'),
            notification.message.index('打开关注搜索查看详情') + 100,
        )