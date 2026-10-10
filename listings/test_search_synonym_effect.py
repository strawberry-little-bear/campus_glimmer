# -*- coding: utf-8 -*-
"""Cover the feedback loop between a confirmed synonym and the search log.

The loop exists because a one-way pipeline cannot correct itself: candidates
are mined, confirmed, and never heard from again, so the mining thresholds stay
whatever somebody first guessed. What these tests pin down is the honesty of
the measurement rather than its arithmetic.

Three things must hold. The measured term is the one students actually type,
because search expansion rewrites the query internally but the SearchQuery row
keeps the original wording - measuring the expanded term would compare a term
against itself and always report success. A synonym that has been switched off
is never scored, since crediting a disabled pair with an improvement is the
easiest way to make the module lie. And a pair with thin evidence is held back
rather than folded into either the successes or the failures, because a
zero-result rate computed from one search is a rounding error.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .search_synonym_effect import (
    DEFAULT_WINDOW_DAYS,
    build_search_synonym_effects,
    build_synonym_effect,
    DEFAULT_DAYS,
    DEFAULT_LIMIT,
    EFFECT_LABELS,
    _affected_terms,
)

from .models import Category, CampusLocation, SearchQuery, SearchSynonym


class SynonymEffectMeasurementTests(TestCase):
    """Which searches are counted, and from which moment."""

    def setUp(self):
        self.user = User.objects.create_user(username='syn-eff-user', password='safe-password-123')
        self.now = timezone.now()
        self.category = Category.objects.create(name='同义词效果分类')
        self.location = CampusLocation.objects.create(name='同义词效果地点')

    def make_synonym(self, *, keyword='充电宝', synonym='移动电源', active=True, age_days=30):
        synonym_row = SearchSynonym.objects.create(
            keyword=keyword, synonym=synonym, is_active=active,
        )
        created_at = self.now - timedelta(days=age_days)
        SearchSynonym.objects.filter(pk=synonym_row.pk).update(created_at=created_at)
        synonym_row.refresh_from_db()
        return synonym_row

    # AGE/WINDOW: a pair confirmed 30 days ago is measured over the 14 days
    # either side of that moment, so a baseline search sits 30-44 days back
    # and an observation search 16-30 days back. The helpers below are called
    # with those offsets, not with ones that straddle the confirmation.
    def search(self, term, *, days_ago, result_count):
        """One recorded search for `term`, `days_ago` before now."""
        query = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count,
            category=self.category, location=self.location,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_a_confirmed_pair_that_fixes_the_dead_end_reports_a_drop(self):
        # 这一条是模块存在的理由：确认前这个词一直搜不到东西，确认后能搜到了，
        # 回流就必须把这个下降说出来。
        synonym_row = self.make_synonym(age_days=30)
        for offset in range(3):
            self.search('充电宝', days_ago=42 - offset, result_count=0)
        for offset in range(3):
            self.search('充电宝', days_ago=28 - offset, result_count=2)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['baseline_zero_rate'], 100.0)
        self.assertEqual(row['observation_zero_rate'], 0.0)
        self.assertEqual(row['delta_points'], 100.0)
        self.assertEqual(row['outcome'], 'effective')
        self.assertEqual(row['outcome_label'], EFFECT_LABELS['effective'])

    def test_both_sides_of_the_pair_are_measured(self):
        # 扩展是双向的：学生可能用任意一侧的说法进来，只统计主关键词会漏掉
        # 另一半流量。
        synonym_row = self.make_synonym(age_days=30)
        self.search('充电宝', days_ago=42, result_count=0)
        self.search('充电宝', days_ago=41, result_count=0)
        self.search('移动电源', days_ago=40, result_count=0)
        self.search('移动电源', days_ago=28, result_count=3)
        self.search('移动电源', days_ago=27, result_count=3)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['baseline_searches'], 3)
        self.assertEqual(row['observation_searches'], 2)

    def test_a_pair_that_makes_search_worse_is_flagged(self):
        # 配错的词对会让搜索结果变差，这种情况必须被点名，而不是被当成持平
        # 混过去——它是运营最需要回头核的那一批。
        synonym_row = self.make_synonym(keyword='iPhone', synonym='手机壳', age_days=30)
        self.search('iPhone', days_ago=42, result_count=4)
        self.search('iPhone', days_ago=41, result_count=4)
        self.search('iPhone', days_ago=28, result_count=0)
        self.search('iPhone', days_ago=27, result_count=0)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['outcome'], 'worse')
        self.assertIn('配错', row['verdict'])


    def test_a_disabled_synonym_is_never_scored(self):
        # 停用后的搜索变化属于市场，不属于这条配置。把停用的词对算成效果，
        # 是这个模块能撒谎的最省事的一条路。
        synonym_row = self.make_synonym(active=False, age_days=30)
        self.search('充电宝', days_ago=42, result_count=0)
        self.search('充电宝', days_ago=41, result_count=0)
        self.search('充电宝', days_ago=28, result_count=2)
        self.search('充电宝', days_ago=27, result_count=2)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['outcome'], 'pending')
        self.assertIsNone(row['delta_points'])
        self.assertIn('停用', row['verdict'])

    def test_a_synonym_too_new_to_judge_waits(self):
        # 刚确认三天的同义词，观察期一大半还没发生，此时算出来的率会好得不真实。
        synonym_row = self.make_synonym(age_days=3)
        self.search('充电宝', days_ago=3, result_count=0)
        self.search('充电宝', days_ago=2, result_count=2)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['outcome'], 'pending')
        self.assertFalse(row['is_mature'])

    def test_a_quiet_term_is_held_back_rather_than_scored(self):
        # 一次搜索算出来的率不是 0% 就是 100%，把它算成生效或失败都是在自欺。
        synonym_row = self.make_synonym(age_days=30)
        self.search('充电宝', days_ago=42, result_count=0)
        self.search('充电宝', days_ago=28, result_count=2)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['outcome'], 'insufficient')
        self.assertEqual(row['outcome_label'], EFFECT_LABELS['insufficient'])

    def test_the_original_wording_is_what_gets_measured(self):
        # 扩展只在查询内部发生，SearchQuery 记的是学生原本输入的那一串。
        # 如果这里统计的是扩展后的词，就等于自己和自己比，永远报成功。
        self.assertEqual(_affected_terms(self.make_synonym()), ['充电宝', '移动电源'])

    def test_a_flat_rate_is_not_reported_as_a_success(self):
        # 前后都是 50%，属于持平，不该被算进命中率里凑数。
        synonym_row = self.make_synonym(age_days=30)
        self.search('充电宝', days_ago=42, result_count=0)
        self.search('充电宝', days_ago=41, result_count=2)
        self.search('充电宝', days_ago=28, result_count=0)
        self.search('充电宝', days_ago=27, result_count=2)

        row = build_synonym_effect(synonym_row, now=self.now)

        self.assertEqual(row['delta_points'], 0)
        self.assertEqual(row['outcome'], 'flat')

    def test_the_summary_leads_with_the_pairs_that_hurt(self):
        # 汇总句要先说最需要动手的那件事：有没有词对把搜索弄差了。
        good = self.make_synonym(keyword='台灯', synonym='桌面灯', age_days=30)
        bad = self.make_synonym(keyword='iPhone', synonym='手机壳', age_days=29)
        for offset in range(3):
            self.search('台灯', days_ago=42 - offset, result_count=0)
            self.search('iPhone', days_ago=41 - offset, result_count=4)
        for offset in range(3):
            self.search('台灯', days_ago=28 - offset, result_count=2)
            self.search('iPhone', days_ago=27 - offset, result_count=0)

        report = build_search_synonym_effects(now=self.now)

        self.assertEqual(report['summary']['worse_count'], 1)
        self.assertEqual(report['summary']['effective_count'], 1)
        self.assertIn('让无结果率上升', report['summary_text'])
        self.assertEqual(report['rows'][0]['keyword'], 'iPhone')

    def test_the_hit_rate_excludes_thin_samples(self):
        # 一条没人搜过的同义词既不算成功也不算失败，否则命中率会被冷门词对
        # 拖着走。
        good = self.make_synonym(keyword='台灯', synonym='桌面灯', age_days=30)
        quiet = self.make_synonym(keyword='冷门词', synonym='另一个冷门词', age_days=29)
        for offset in range(3):
            self.search('台灯', days_ago=42 - offset, result_count=0)
        for offset in range(3):
            self.search('台灯', days_ago=28 - offset, result_count=2)

        report = build_search_synonym_effects(now=self.now)

        self.assertEqual(report['summary']['judged_count'], 1)
        self.assertEqual(report['summary']['insufficient_count'], 1)
        self.assertEqual(report['summary']['hit_rate'], 100.0)

    def test_nothing_to_report_when_no_synonym_was_confirmed(self):
        report = build_search_synonym_effects(now=self.now)

        self.assertFalse(report['has_data'])
        self.assertIn('没有新确认的同义词', report['summary_text'])

    def test_defaults_are_exported_for_the_page(self):
        self.assertGreater(DEFAULT_WINDOW_DAYS, 0)
        self.assertGreater(DEFAULT_DAYS, DEFAULT_WINDOW_DAYS)
        self.assertGreater(DEFAULT_LIMIT, 0)
        self.assertIn('effective', EFFECT_LABELS)

