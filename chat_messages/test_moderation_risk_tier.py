# -*- coding: utf-8 -*-
"""内容审核分级与复核节奏测试。

分级只决定人工查看顺序和目标节奏，不能改变分数、风险等级，也不能替代管理员
的最终判定；这些测试同时守住持久化、历史回填和后台只读边界。
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from listings.models import Category, Item
from .admin import ModerationEventAdmin
from .models import ModerationEvent
from .moderation import analyze_content, moderate_submission, risk_tier_for_labels


def load_risk_tier_migration():
    path = Path(__file__).with_name('migrations').joinpath('0007_moderationevent_risk_tier.py')
    spec = importlib.util.spec_from_file_location('campus_glimmer_risk_tier_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModerationRiskTierAnalysisTests(TestCase):
    def test_external_link_is_a_caution_tier(self):
        analysis = analyze_content('课程资料在 https://example.com/course.pdf')

        self.assertEqual(analysis['matched_terms'], ['外部链接'])
        self.assertEqual(analysis['risk_tier'], 'caution')
        self.assertEqual(analysis['risk_score'], 20)
        self.assertEqual(analysis['risk_level'], 'low')

    def test_off_platform_contact_is_an_elevated_tier(self):
        analysis = analyze_content('请加微信，手机号 13812345678')

        self.assertEqual(analysis['matched_terms'], ['微信导流', '手机号'])
        self.assertEqual(analysis['risk_tier'], 'elevated')
        self.assertEqual(analysis['risk_score'], 75)
        self.assertEqual(analysis['risk_level'], 'high')

    def test_high_risk_trade_is_a_critical_tier(self):
        analysis = analyze_content('需要先付押金')

        self.assertEqual(analysis['matched_terms'], ['高风险交易'])
        self.assertEqual(analysis['risk_tier'], 'critical')
        self.assertEqual(analysis['risk_score'], 50)
        self.assertEqual(analysis['risk_level'], 'medium')

    def test_multiple_rules_use_the_highest_tier_without_rewriting_score(self):
        analysis = analyze_content('请发微信号，手机号 13812345678，押金和 https://example.com')

        self.assertEqual(analysis['matched_terms'], ['微信导流', '外部链接', '手机号', '高风险交易'])
        self.assertEqual(analysis['risk_tier'], 'critical')
        self.assertEqual(analysis['risk_score'], 100)
        self.assertEqual(analysis['risk_level'], 'high')

    def test_clean_content_does_not_invent_a_tier(self):
        self.assertEqual(analyze_content('正常交易'), {
            'matched_terms': [],
            'risk_score': 0,
            'risk_level': 'low',
            'risk_tier': None,
        })

    def test_unknown_historical_labels_stay_caution(self):
        self.assertEqual(risk_tier_for_labels(['已下线规则']), 'caution')
        self.assertEqual(risk_tier_for_labels(None), 'caution')


class ModerationRiskTierPersistenceTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='tier-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='tier-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='审核分级测试')
        self.item = Item.objects.create(
            title='分级测试商品', description='测试商品', price='10.00',
            category=self.category, condition='九成新', seller=self.seller,
        )

    def test_new_submission_persists_its_highest_tier(self):
        event = moderate_submission(
            '请加微信后私下转账',
            author=self.buyer, channel='comment', item=self.item,
        )

        event.refresh_from_db()
        self.assertEqual(event.matched_terms, '微信导流、高风险交易')
        self.assertEqual(event.risk_tier, 'critical')
        self.assertEqual(event.risk_score, 90)
        self.assertEqual(event.risk_level, 'high')

    def test_historical_backfill_uses_saved_labels_without_reading_content(self):
        migration = load_risk_tier_migration()
        records = [
            type('Record', (), {'matched_terms': '高风险交易、外部链接', 'risk_tier': None})(),
            type('Record', (), {'matched_terms': '微信导流', 'risk_tier': None})(),
            type('Record', (), {'matched_terms': '手机号', 'risk_tier': None})(),
            type('Record', (), {'matched_terms': '外部链接', 'risk_tier': None})(),
            type('Record', (), {'matched_terms': '已下线规则', 'risk_tier': None})(),
        ]

        class Manager:
            def __init__(self, rows):
                self.rows = rows

            def all(self):
                return list(self.rows)

            def bulk_update(self, rows, fields, batch_size=None):
                self.captured_fields = fields
                self.captured_batch_size = batch_size
                self.updated_rows = rows

        manager = Manager(records)
        model = type('Model', (), {'objects': manager})()

        class FakeApps:
            def get_model(self, app_label, model_name):
                return model

        migration.backfill_risk_tiers(FakeApps(), None)

        self.assertEqual(manager.captured_fields, ['risk_tier'])
        self.assertEqual(manager.captured_batch_size, 500)
        self.assertEqual([row.risk_tier for row in records], [
            'critical', 'elevated', 'elevated', 'caution', 'caution',
        ])


class ModerationReviewTimingTests(TestCase):
    def make_event(self, *, tier='critical', created_at=None):
        event = ModerationEvent.objects.create(
            channel='comment', content='测试内容', matched_terms='高风险交易',
            risk_score=50, risk_level='medium', risk_tier=tier,
        )
        if created_at is not None:
            ModerationEvent.objects.filter(id=event.id).update(created_at=created_at)
            event.refresh_from_db()
        return event

    def test_timing_states_follow_each_tier_target(self):
        now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        with patch('chat_messages.models.timezone.now', return_value=now):
            critical_on_time = self.make_event(created_at=now - timedelta(hours=1))
            critical_due_soon = self.make_event(created_at=now - timedelta(hours=3))
            critical_overdue = self.make_event(created_at=now - timedelta(hours=4, seconds=1))
            elevated_due_soon = self.make_event(
                tier='elevated', created_at=now - timedelta(hours=9),
            )
            caution_on_time = self.make_event(
                tier='caution', created_at=now - timedelta(hours=10),
            )

            self.assertEqual(critical_on_time.review_timing_state, 'on_time')
            self.assertEqual(critical_due_soon.review_timing_state, 'due_soon')
            self.assertEqual(critical_overdue.review_timing_state, 'overdue')
            self.assertEqual(elevated_due_soon.review_timing_state, 'due_soon')
            self.assertEqual(caution_on_time.review_timing_state, 'on_time')
            self.assertEqual(critical_on_time.review_target_hours, 4)
            self.assertEqual(elevated_due_soon.review_target_hours, 12)
            self.assertEqual(caution_on_time.review_target_hours, 48)

    def test_closed_events_do_not_claim_review_timing(self):
        now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        with patch('chat_messages.models.timezone.now', return_value=now):
            event = self.make_event(created_at=now - timedelta(days=3))
            event.status = 'confirmed'

            self.assertIsNone(event.review_timing_state)


class ModerationRiskTierQueueTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='tier-staff', password='safe-password-123', is_staff=True,
        )
        self.buyer = User.objects.create_user(username='tier-queue-buyer', password='safe-password-123')
        self.client.login(username='tier-staff', password='safe-password-123')

    def make_event(self, *, tier, status='pending', score=20):
        return ModerationEvent.objects.create(
            channel='comment', author=self.buyer, content=f'{tier} 内容',
            matched_terms='外部链接', risk_score=score, risk_level='low',
            risk_tier=tier, status=status,
        )

    def test_queue_orders_pending_records_by_tier_before_closed_records(self):
        caution = self.make_event(tier='caution', score=90)
        elevated = self.make_event(tier='elevated', score=80)
        critical = self.make_event(tier='critical', score=50)
        closed = self.make_event(tier='critical', status='confirmed', score=100)

        response = self.client.get(reverse('moderation_queue'), {'status': 'all'})

        self.assertEqual(list(response.context['moderation_events']), [
            critical, elevated, caution, closed,
        ])
        self.assertEqual(response.context['moderation_tier_filter'], 'all')

    def test_queue_filters_by_tier_and_exposes_counts(self):
        critical = self.make_event(tier='critical', score=50)
        elevated = self.make_event(tier='elevated', score=75)
        self.make_event(tier='caution', score=20)

        response = self.client.get(reverse('moderation_queue'), {'tier': 'critical'})

        self.assertEqual(list(response.context['moderation_events']), [critical])
        self.assertEqual(response.context['moderation_tier_filter'], 'critical')
        self.assertEqual(response.context['moderation_pending_critical_count'], 1)
        self.assertEqual(response.context['moderation_pending_elevated_count'], 1)
        self.assertEqual(response.context['moderation_pending_caution_count'], 1)
        self.assertContains(response, '严重风险 · 4小时目标')

        fallback = self.client.get(reverse('moderation_queue'), {'tier': 'unknown'})
        self.assertEqual(fallback.context['moderation_tier_filter'], 'all')
        self.assertEqual(len(fallback.context['moderation_events']), 3)

    def test_queue_overdue_counts_use_tier_targets(self):
        now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        with patch('chat_messages.views.timezone.now', return_value=now):
            critical = self.make_event(tier='critical')
            ModerationEvent.objects.filter(id=critical.id).update(
                created_at=now - timedelta(hours=5),
            )
            elevated = self.make_event(tier='elevated')
            ModerationEvent.objects.filter(id=elevated.id).update(
                created_at=now - timedelta(hours=13),
            )
            caution = self.make_event(tier='caution')
            ModerationEvent.objects.filter(id=caution.id).update(
                created_at=now - timedelta(hours=49),
            )

            response = self.client.get(reverse('moderation_queue'), {'status': 'all'})

        self.assertEqual(response.context['moderation_overdue_critical_count'], 1)
        self.assertEqual(response.context['moderation_overdue_elevated_count'], 1)
        self.assertEqual(response.context['moderation_overdue_caution_count'], 1)


class ModerationRiskTierAdminTests(TestCase):
    def test_admin_exposes_tier_as_read_only_and_filterable(self):
        admin = ModerationEventAdmin(ModerationEvent, None)

        self.assertIn('risk_tier', admin.list_display)
        self.assertIn('risk_tier', admin.list_filter)
        self.assertIn('risk_tier', admin.readonly_fields)
