# -*- coding: utf-8 -*-
"""Cover the step between a hit rate and the threshold that gates candidates.

The feedback loop only half-closes without this step: the effect module can say
how many confirmed synonyms moved the zero-result rate, but that number changes
nothing while the gate that decides which candidates an operator ever sees is a
constant nobody revisits. What these tests pin down is not the arithmetic - it
is the discipline of the recommendation.

Four things must hold. A pool that mostly backfires must not be answered with a
threshold change, because tightening then hides wrong pairs instead of getting
them fixed. Thin evidence must produce no direction at all, since a hit rate of
one-out-of-one is an anecdote. A suggestion must never be the value already in
use, because naming the current setting as a change is a recommendation that
cannot be acted on. And the module must never write to the constant it is
advising about, which is the one boundary that keeps this an operator's decision
rather than the platform's.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import SearchQuery, SearchSynonym
from .search_rewrites import MIN_OCCURRENCES
from .search_threshold_feedback import (
    FLAT_SHARE_LIMIT,
    LOOSEN_HIT_RATE,
    MIN_JUDGED_SYNONYMS,
    THRESHOLD_LADDER,
    TIGHTEN_HIT_RATE,
    build_search_threshold_feedback,
)


class ThresholdRecommendationTests(TestCase):
    """Which direction the gate is said to be wrong, and when it is not.

    The fixtures follow the effect module's timing. A synonym confirmed
    `age_days` ago is measured over the 14 days either side of that moment, so
    a baseline search sits 44-30 days back and an observation search 30-16 days
    back. Searches that straddle the confirmation moment would be counted on
    both sides and would make every assertion below wrong.
    """

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='rec-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='rec-other', password='safe-password-123',
        )

    def make_synonym(self, keyword, synonym, age_days=30, active=True):
        row = SearchSynonym.objects.create(
            keyword=keyword, synonym=synonym, is_active=active,
        )
        SearchSynonym.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(days=age_days),
        )
        row.refresh_from_db()
        return row

    def search(self, term, *, days_ago, result_count, user=None):
        query = SearchQuery.objects.create(
            user=user or self.user, query=term, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def _improving(self, keyword, synonym):
        self.make_synonym(keyword, synonym)
        for offset in range(3):
            self.search(keyword, days_ago=42 - offset, result_count=0)
        for offset in range(3):
            self.search(keyword, days_ago=28 - offset, result_count=3)

    def _worsening(self, keyword, synonym):
        self.make_synonym(keyword, synonym)
        for offset in range(3):
            self.search(keyword, days_ago=42 - offset, result_count=4)
        for offset in range(3):
            self.search(keyword, days_ago=28 - offset, result_count=0)

    def _flat(self, keyword, synonym):
        self.make_synonym(keyword, synonym)
        for offset in range(3):
            self.search(keyword, days_ago=42 - offset, result_count=0 if offset % 2 else 2)
        for offset in range(3):
            self.search(keyword, days_ago=28 - offset, result_count=0 if offset % 2 else 2)

    def test_a_pool_of_backfiring_pairs_is_not_answered_with_a_threshold(self):
        # 这一条决定这个模块不会帮倒忙：词对本身配错的时候，收紧门槛只会把
        # 错误藏起来不给人看，而不是修好它。
        for index in range(MIN_JUDGED_SYNONYMS):
            self._worsening('错词%d' % index, '配错%d' % index)

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'misconfigured')
        self.assertIsNone(report['recommendation']['suggested_threshold'])
        self.assertIsNone(report['suggested_volume'])
        self.assertIn('词对本身', report['recommendation']['reason'])

    def test_no_judged_synonym_produces_no_direction(self):
        # 一条可判定的同义词都没有时，命中率是无，任何方向都是编的。
        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'insufficient')
        self.assertIsNone(report['recommendation']['suggested_threshold'])
        self.assertFalse(report['has_data'])
        self.assertIn(str(MIN_JUDGED_SYNONYMS), report['recommendation']['reason'])

    def test_a_hit_rate_below_the_floor_says_to_tighten(self):
        # 确认过的词对大多没起作用，说明门槛放进来的候选噪声偏多。
        for index in range(MIN_JUDGED_SYNONYMS):
            self._flat('持平词%d' % index, '另一说法%d' % index)
        self._improving('台灯', '桌面灯')

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'tighten')
        self.assertEqual(
            report['recommendation']['suggested_threshold'], MIN_OCCURRENCES + 1,
        )
        self.assertIn(str(TIGHTEN_HIT_RATE), report['recommendation']['reason'])

    def test_a_high_hit_rate_says_to_loosen(self):
        # 运营确认过的词对大多是真实等价，就该把门槛放低一点，别把低频改口挡在外面。
        for index in range(MIN_JUDGED_SYNONYMS):
            self._improving('生效词%d' % index, '等价说法%d' % index)

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'loosen')
        self.assertEqual(
            report['recommendation']['suggested_threshold'], MIN_OCCURRENCES - 1,
        )
        self.assertIn(str(LOOSEN_HIT_RATE), report['recommendation']['reason'])

    def test_a_pool_that_is_half_flat_tightens_even_with_a_decent_hit_rate(self):
        # 命中率还在区间里，但一半的词对不升不降，说明这些不是等价关系。
        # 只看命中率会漏掉这种情况。
        for index in range(3):
            self._improving('生效词%d' % index, '等价说法%d' % index)
        for index in range(MIN_JUDGED_SYNONYMS):
            self._flat('持平词%d' % index, '另一说法%d' % index)

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'tighten')
        self.assertIn(str(FLAT_SHARE_LIMIT), report['recommendation']['reason'])

    def test_a_balanced_pool_holds(self):
        # 命中率落在合理区间、持平比例也没超标，就不该为改而改。
        # 三条生效配两条持平：命中率 60% 在 40%–70% 之间，持平占 40% 未到 50%
        # 的收紧线。这个组合说的正是「有效果、也有一批看不出名堂」，是正常状态。
        for index in range(3):
            self._improving('生效词%d' % index, '等价说法%d' % index)
        for index in range(2):
            self._flat('持平词%d' % index, '另一说法%d' % index)

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(report['recommendation']['action'], 'hold')
        self.assertIsNone(report['recommendation']['suggested_threshold'])
        self.assertIsNone(report['suggested_volume'])

    def test_a_suggestion_is_never_the_value_already_in_use(self):
        # 建议的档位不能就是当前档位，否则这条建议无法执行。
        for index in range(MIN_JUDGED_SYNONYMS):
            self._worsening('错词%d' % index, '配错%d' % index)

        for action in ('loosen', 'tighten'):
            with self.subTest(action=action):
                report = build_search_threshold_feedback(now=self.now)
                if report['recommendation']['action'] == action:
                    self.assertNotEqual(
                        report['recommendation']['suggested_threshold'],
                        MIN_OCCURRENCES,
                    )

    def test_the_module_never_writes_the_threshold_it_advises_about(self):
        # 这是唯一一条不能破的边界：模块只建议，绝不自己改常量。
        from listings import search_rewrites
        before = search_rewrites.MIN_OCCURRENCES

        for index in range(MIN_JUDGED_SYNONYMS):
            self._worsening('错词%d' % index, '配错%d' % index)
        build_search_threshold_feedback(now=self.now)
        build_search_threshold_feedback(now=self.now)

        self.assertEqual(search_rewrites.MIN_OCCURRENCES, before)

    def test_the_candidate_volumes_cover_the_whole_ladder(self):
        # 每档都要算一遍候选量，否则运营看不到改一档到底会多出多少活。
        for index in range(MIN_JUDGED_SYNONYMS):
            self._worsening('错词%d' % index, '配错%d' % index)

        report = build_search_threshold_feedback(now=self.now)

        self.assertEqual(
            [row['threshold'] for row in report['threshold_volumes']],
            list(THRESHOLD_LADDER),
        )
        self.assertTrue(
            all(row['is_current'] for row in report['threshold_volumes']
                if row['threshold'] == MIN_OCCURRENCES)
        )
        # 门槛越高，候选越少，这是单调的，反过来就说明算错了。
        totals = [row['total'] for row in report['threshold_volumes']]
        self.assertEqual(totals, sorted(totals, reverse=True))


class ThresholdPanelTests(TestCase):
    """The recommendation has to reach the two places an operator works in.

    A module that computes the right answer and then leaves it in the database
    is indistinguishable from one that was never written, so these tests are
    about visibility rather than arithmetic.
    """

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='panel-user', password='safe-password-123',
        )

    def _staff(self, username):
        return User.objects.create_superuser(
            username=username, email=f'{username}@example.com',
            password='safe-password-123',
        )

    def make_synonym(self, keyword, synonym, age_days=30):
        row = SearchSynonym.objects.create(keyword=keyword, synonym=synonym)
        SearchSynonym.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(days=age_days),
        )
        row.refresh_from_db()
        return row

    def search(self, term, *, days_ago, result_count):
        query = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def _seed_backfiring_pool(self):
        for index in range(MIN_JUDGED_SYNONYMS):
            self.make_synonym('错词%d' % index, '配错%d' % index)
            for offset in range(3):
                self.search('错词%d' % index, days_ago=42 - offset, result_count=4)
            for offset in range(3):
                self.search('错词%d' % index, days_ago=28 - offset, result_count=0)

    def test_the_recommendation_appears_on_the_insights_page(self):
        self._seed_backfiring_pool()
        self.client.force_login(self._staff('threshold-view-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 180})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '候选门槛建议')
        self.assertContains(response, '词对本身')

    def test_the_page_states_that_the_threshold_is_not_auto_changed(self):
        # 「只建议、不自动改」必须写在页面上。运营看到一条建议时，有权知道
        # 它不会自己生效。
        self._seed_backfiring_pool()
        self.client.force_login(self._staff('threshold-note-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 180})

        self.assertContains(response, '不自动')

    def test_the_page_explains_the_judgement_limitation(self):
        # 命中率衡量的是「候选池 + 人工确认」这一整条链，不是候选池本身。
        # 这个局限必须写明，否则运营会把一个判断失误当成候选质量问题。
        self._seed_backfiring_pool()
        self.client.force_login(self._staff('threshold-limit-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 180})

        self.assertContains(response, '人工确认')

    def test_the_recommendation_is_included_in_csv_export(self):
        self._seed_backfiring_pool()
        self.client.force_login(self._staff('threshold-export-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 180})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '候选门槛建议')
        self.assertContains(export, '当前门槛')

    def test_the_export_shows_every_rung_of_the_ladder(self):
        # 导出要能带走整条档位表，运营才能和同事讨论要不要换档。
        self._seed_backfiring_pool()
        self.client.force_login(self._staff('threshold-ladder-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 180})
        content = export.content.decode('utf-8-sig')

        self.assertIn('档位候选量', content)
        for value in THRESHOLD_LADDER:
            self.assertIn(str(value), content)
