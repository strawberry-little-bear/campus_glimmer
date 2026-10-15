# -*- coding: utf-8 -*-
"""钉住学期阶段切换前的供给预热口径。

这些测试重点不是预测需求，而是守住四件事：阶段必须真的临近、基准必须是其它学期
同阶段、分类提示必须有样本门槛、页面和 CSV 不泄露任何搜索词。
"""

import csv
import io
from datetime import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .academic_phase_supply_preparation import (
    build_academic_phase_supply_preparation,
)
from .models import AcademicPhase, AcademicTerm, Category, DemandPost, Item, SearchQuery


class AcademicPhaseSupplyPreparationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='phase-preparation-seller', password='safe-password-123',
        )
        self.now = timezone.make_aware(
            datetime(2026, 10, 15, 10, 0), timezone.get_current_timezone(),
        )
        self.today = timezone.localdate(self.now)

    def make_term(self, name, slug, kind, starts_on, ends_on):
        return AcademicTerm.objects.create(
            name=name, slug=slug, kind=kind,
            starts_on=starts_on, ends_on=ends_on, is_active=True,
        )

    def make_item(self, category, created_at, *, title='阶段预热测试商品'):
        item = Item.objects.create(
            title=title,
            description='用于阶段供给预热统计',
            trade_mode='sale',
            price='20.00',
            category=category,
            condition='九成新',
            seller=self.seller,
            status='available',
        )
        Item.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def make_search(self, category, created_at, result_count, query='阶段预热测试搜索词'):
        search = SearchQuery.objects.create(
            query=query, category=category, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=search.pk).update(created_at=created_at)
        return search

    def make_demand(self, category, created_at):
        demand = DemandPost.objects.create(
            requester=self.seller,
            title='阶段预热测试求购',
            description='用于阶段供给预热统计',
            category=category,
            status='active',
        )
        DemandPost.objects.filter(pk=demand.pk).update(created_at=created_at)
        return demand

    def test_no_upcoming_phase_is_reported_without_a_fake_baseline(self):
        self.make_term('2026 春季', 'prep-2026-spring', 'spring', '2026-02-20', '2026-06-30')
        report = build_academic_phase_supply_preparation(now=self.now)

        self.assertFalse(report['has_upcoming_phase'])
        self.assertFalse(report['has_benchmark'])
        self.assertIn('14 天内', report['empty_reason'])
        self.assertEqual(report['rows'] if 'rows' in report else [], [])

    def test_selects_the_next_phase_and_a_closed_same_kind_phase(self):
        current = self.make_term(
            '2026 秋季', 'prep-2026-autumn', 'autumn', '2026-10-01', '2027-02-28',
        )
        AcademicPhase.objects.create(
            term=current, phase='regular', start_offset=0, end_offset=14,
        )
        AcademicPhase.objects.create(
            term=current, phase='exam', start_offset=15, end_offset=28,
        )
        same_kind = self.make_term(
            '2025 秋季', 'prep-2025-autumn', 'autumn', '2025-10-01', '2026-02-28',
        )
        AcademicPhase.objects.create(
            term=same_kind, phase='exam', start_offset=15, end_offset=28,
        )
        other_kind = self.make_term(
            '2026 春季', 'prep-nearer-spring', 'spring', '2026-02-20', '2026-06-30',
        )
        AcademicPhase.objects.create(
            term=other_kind, phase='exam', start_offset=0, end_offset=13,
        )

        report = build_academic_phase_supply_preparation(now=self.now)

        self.assertTrue(report['has_upcoming_phase'])
        self.assertEqual(report['upcoming_phase_label'], '考试周')
        self.assertEqual(report['days_until'], 1)
        self.assertTrue(report['has_benchmark'])
        self.assertEqual(report['benchmark_term_name'], '2025 秋季')
        self.assertTrue(report['benchmark_kind_matched'])

    def test_candidate_thresholds_and_seven_day_pace_use_category_rows(self):
        current = self.make_term(
            '2026 秋季', 'prep-pace-autumn', 'autumn', '2026-10-01', '2027-02-28',
        )
        AcademicPhase.objects.create(
            term=current, phase='exam', start_offset=15, end_offset=28,
        )
        previous = self.make_term(
            '2025 秋季', 'prep-pace-2025', 'autumn', '2025-10-01', '2026-02-28',
        )
        benchmark_phase = AcademicPhase.objects.create(
            term=previous, phase='exam', start_offset=15, end_offset=28,
        )
        strong = Category.objects.create(name='阶段预热复习资料')
        weak = Category.objects.create(name='阶段预热日常杂货')
        historical = timezone.make_aware(
            datetime(2025, 10, 20, 9, 0), timezone.get_current_timezone(),
        )
        for result_count in (0, 0, 3):
            self.make_search(strong, historical, result_count)
        self.make_search(weak, historical, 0)
        self.make_demand(strong, historical)
        for index in range(14):
            self.make_item(
                strong, historical + timezone.timedelta(hours=index),
                title=f'历史复习资料 {index}',
            )

        for index in range(5):
            self.make_item(
                strong, self.now - timezone.timedelta(hours=index + 1),
                title=f'当前复习资料 {index}',
            )

        report = build_academic_phase_supply_preparation(days=90, now=self.now)
        rows = {row['category_name']: row for row in report['rows']}

        self.assertEqual(report['benchmark_day_count'], 14)
        self.assertEqual(rows['阶段预热复习资料']['search_count'], 3)
        self.assertEqual(rows['阶段预热复习资料']['zero_result_count'], 2)
        self.assertEqual(rows['阶段预热复习资料']['zero_result_share'], 66.7)
        self.assertEqual(rows['阶段预热复习资料']['benchmark_item_count'], 14)
        self.assertEqual(rows['阶段预热复习资料']['lead_baseline_items'], 7)
        self.assertEqual(rows['阶段预热复习资料']['lead_item_count'], 5)
        self.assertEqual(rows['阶段预热复习资料']['available_item_count'], 19)
        self.assertTrue(rows['阶段预热复习资料']['is_guidance_candidate'])
        self.assertEqual(rows['阶段预热复习资料']['pace_key'], 'slow')
        self.assertFalse(rows['阶段预热日常杂货']['is_guidance_candidate'])
        self.assertEqual(report['guidance_total'], 1)
        self.assertEqual(report['slow_total'], 1)
        self.assertLessEqual(len(report['page_rows']), report['page_row_limit'])

    def test_missing_comparable_phase_is_an_explicit_empty_state(self):
        current = self.make_term(
            '2026 秋季', 'prep-no-baseline', 'autumn', '2026-10-01', '2027-02-28',
        )
        AcademicPhase.objects.create(
            term=current, phase='graduation', start_offset=15, end_offset=28,
        )
        report = build_academic_phase_supply_preparation(now=self.now)

        self.assertTrue(report['has_upcoming_phase'])
        self.assertFalse(report['has_benchmark'])
        self.assertIn('先不造基线', report['empty_reason'])
        self.assertIn('缺少可比较', report['summary'])
        self.assertNotIn('rows', report)

    def test_page_and_export_share_the_panel_without_search_terms(self):
        staff = User.objects.create_user(
            username='phase-preparation-staff', password='safe-password-123', is_staff=True,
        )
        current = self.make_term(
            '2026 秋季', 'prep-export-autumn', 'autumn', '2026-10-01', '2027-02-28',
        )
        AcademicPhase.objects.create(
            term=current, phase='exam', start_offset=15, end_offset=28,
        )
        previous = self.make_term(
            '2025 秋季', 'prep-export-2025', 'autumn', '2025-10-01', '2026-02-28',
        )
        AcademicPhase.objects.create(
            term=previous, phase='exam', start_offset=15, end_offset=28,
        )
        category = Category.objects.create(name='阶段预热导出分类')
        historical = timezone.make_aware(
            datetime(2025, 10, 20, 9, 0), timezone.get_current_timezone(),
        )
        for result_count in (0, 0, 2):
            self.make_search(category, historical, result_count, query='不要导出的搜索词')
        self.make_item(category, historical)

        self.client.force_login(staff)
        page = self.client.get(reverse('operations_dashboard'))
        export = self.client.get(reverse('operations_dashboard_export'))
        page_text = page.content.decode('utf-8')
        export_text = export.content.decode('utf-8-sig')

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertIn('学期阶段供给预热', page_text)
        self.assertFalse('不要导出的搜索词' in page_text)
        self.assertNotIn('不要导出的搜索词', export_text)
        sections = {
            row[0] for row in csv.reader(io.StringIO(export_text)) if row
        }
        self.assertIn('学期阶段供给预热', sections)
