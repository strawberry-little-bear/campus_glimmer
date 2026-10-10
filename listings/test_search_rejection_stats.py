# -*- coding: utf-8 -*-
"""Cover the dismissed half of the synonym candidate pipeline.

确认和否决必须被同等对待，这条链路才算闭上。此前候选只能被确认：被运营
看过并否掉的词对不留任何痕迹，下周原样回到列表里，运营只能把同一批噪声
再读一遍；而门槛建议只读已确认的词，于是它看到的候选池永远是纯净的——
一个确认了两百条、也默默否掉两百条的池子，和一个只确认了两百条的池子，
在它眼里没有区别。

这些用例守住的不是算术，而是四条边界：

否决必须真的让候选消失，而且是在挖掘里消失，不是在模板里被藏起来。
面板上的候选总数要描述运营真正还要处理多少活，不是一个更长的列表上面
盖了一块布。

否决必须能被撤销。运营三月否掉一个词对、十月改主意是常态，一个只能新增
不能撤销的否决表会把一次误判永久变成一次误判。

被否决的词对不能静默消失。它要连同出现次数一起留在某个运营看得到的地方，
否则否决只是一次没有留痕的删除，事后无法回答「当初为什么否掉它」。

否决不得反过来变成平台行为。它不扩大也不缩小任何搜索的结果范围，更不修改
候选次数门槛本身——那是 `search_threshold_feedback` 的地盘，而那个模块也
同样只建议、不自动改。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import SearchQuery, SearchSynonym, SearchSynonymRejection
from .search_rejection_stats import (
    MIN_JUDGED_REJECTIONS,
    build_search_rejection_stats,
    restore_rejections_for_pair,
    supersede_rejections_for_pair,
)
from .search_rewrites import MIN_OCCURRENCES, build_search_rewrite_candidates


class RejectionRecordingTests(TestCase):
    """否决本身：写进去、读出来、可撤销、可失效。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='reject-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='reject-other', password='safe-password-123',
        )
        self.staff = User.objects.create_superuser(
            username='reject-staff', email='reject-staff@example.com',
            password='safe-password-123',
        )

    def _search(self, user, query, result_count, minutes_ago):
        """按相对分钟数写入搜索记录。

        `SearchQuery.created_at` 是 auto_now_add，创建时传值会被静默丢弃，
        必须先落库再 update 写回，否则所有记录都堆在“现在”。
        """
        record = SearchQuery.objects.create(
            user=user, query=query, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return record

    def _seed_pair(self, source='充电宝', target='移动电源'):
        """造出两次达到门槛的改口，让这个词对真的成为候选。"""
        self._search(self.user, source, 0, 20)
        self._search(self.user, target, 4, 10)
        self._search(self.other_user, source, 0, 8)
        self._search(self.other_user, target, 2, 5)

    def _reject(self, source='充电宝', target='移动电源', reason='not_equivalent'):
        return SearchSynonymRejection.objects.create(
            source=source, target=target, reason=reason, rejected_by=self.staff,
        )

    def test_a_dismissed_pair_stops_being_a_candidate(self):
        """否决必须让候选消失，否则运营下周还要再读一遍同一批噪声。"""
        self._seed_pair()
        self._reject()

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(
            [row for row in report['rows'] if row['source'] == '充电宝'],
            '被否决的词对不应该再出现在候选列表里',
        )
        self.assertEqual(report['summary']['rejected_count'], 1)

    def test_a_dismissed_pair_is_excluded_in_both_directions(self):
        """反方向记录的否决也要生效，否则同一个判断换个顺序就能绕过去。"""
        self._seed_pair()
        self._reject(source='移动电源', target='充电宝')

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(
            [row for row in report['rows'] if row['target'] == '移动电源'],
        )

    def test_undoing_a_dismissal_brings_the_candidate_back(self):
        """撤销否决后候选要回来，改主意是运营的常态而不是异常。"""
        self._seed_pair()
        rejection = self._reject()
        rejection.delete()

        report = build_search_rewrite_candidates(days=7)

        self.assertTrue(
            [row for row in report['rows'] if row['source'] == '充电宝'],
        )
        self.assertEqual(report['summary']['rejected_count'], 0)

    def test_the_view_records_a_dismissal_with_its_reason(self):
        """页面上的否决按钮要落库，否则只是一个好看的装饰。"""
        self._seed_pair()
        self.client.force_login(self.staff)

        response = self.client.post(reverse('reject_rewrite_candidate'), {
            'source': '充电宝', 'target': '移动电源', 'reason': 'not_equivalent',
        })

        self.assertEqual(response.status_code, 302)
        row = SearchSynonymRejection.objects.get(source='充电宝', target='移动电源')
        self.assertEqual(row.reason, 'not_equivalent')
        self.assertEqual(row.rejected_by, self.staff)

    def test_a_dismissal_without_a_reason_is_refused(self):
        """没有原因的否决只是一句“我不喜欢”，对后续判断没有任何用处。"""
        self.client.force_login(self.staff)

        self.client.post(reverse('reject_rewrite_candidate'), {
            'source': '充电宝', 'target': '移动电源',
        })

        self.assertFalse(SearchSynonymRejection.objects.exists())

    def test_a_malformed_pair_is_refused(self):
        """词对不完整时不能猜一个写进去：半归一化的词对会挡错候选。"""
        self.client.force_login(self.staff)

        self.client.post(reverse('reject_rewrite_candidate'), {
            'source': '  ', 'target': '移动电源', 'reason': 'too_rare',
        })
        self.client.post(reverse('reject_rewrite_candidate'), {
            'source': '充电宝', 'target': '充电宝', 'reason': 'too_rare',
        })

        self.assertFalse(SearchSynonymRejection.objects.exists())

    def test_undoing_a_dismissal_through_the_view(self):
        """撤销入口要真的删掉记录，而不是把候选永久藏起来。"""
        self._seed_pair()
        self._reject()
        self.client.force_login(self.staff)

        response = self.client.post(reverse('restore_rewrite_candidate'), {
            'source': '充电宝', 'target': '移动电源',
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SearchSynonymRejection.objects.exists())
        report = build_search_rewrite_candidates(days=7)
        self.assertTrue(
            [row for row in report['rows'] if row['source'] == '充电宝'],
        )

    def test_a_staff_only_endpoint_refuses_ordinary_users(self):
        """否决是运营动作，普通用户点进来必须被拒绝。"""
        self.client.force_login(self.user)

        response = self.client.post(reverse('reject_rewrite_candidate'), {
            'source': '充电宝', 'target': '移动电源', 'reason': 'not_equivalent',
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(SearchSynonymRejection.objects.exists())

    def test_a_confirmed_synonym_supersedes_its_rejection(self):
        """词对后来仍被确认时，否决要失效而不是继续屏蔽候选。"""
        self._seed_pair()
        self._reject()

        updated = supersede_rejections_for_pair('充电宝', '移动电源')

        self.assertEqual(updated, 1)
        row = SearchSynonymRejection.objects.get()
        self.assertTrue(row.is_superseded)
        report = build_search_rewrite_candidates(days=7)
        self.assertTrue(
            [row for row in report['rows'] if row['source'] == '充电宝'],
            '已被确认的词对不该再被一条失效的否决挡着',
        )

    def test_deleting_the_synonym_brings_the_rejection_back(self):
        """同义词被删掉后否决重新生效，否则候选会被一条无关的记录继续挡着。"""
        self._seed_pair()
        self._reject()
        supersede_rejections_for_pair('充电宝', '移动电源')

        restored = restore_rejections_for_pair('充电宝', '移动电源')

        self.assertEqual(restored, 1)
        self.assertFalse(SearchSynonymRejection.objects.get().is_superseded)


class RejectionStatsTests(TestCase):
    """否决统计：频率加权、原因分布，以及它对门槛的说法。"""

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='stats-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='stats-other', password='safe-password-123',
        )

    def _search(self, user, query, result_count, minutes_ago):
        record = SearchQuery.objects.create(
            user=user, query=query, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return record

    def _seed_judged_pair(self, source, target, reason, repeats, slot=0):
        """造出一条达到门槛的否决样本，并返回它。

        `slot` 给这批搜索一个独立的时间片。挖掘规则按「同一用户的相邻
        两条记录」串链，多个词对若挤在同一个用户的同一段时间里，「错
        词0」的下一条就变成「错词1」而不是「配错0」，链条整个断开、一
        条都不成立。真实日志里几次独立的改口本来就分散在不同时刻，所
        以这里让每个词对往后错开 slot * 120 分钟。

        `repeats` 同时决定出现次数： repeats 次改口就是 repeats 次出现，
        门槛由它是否达到 MIN_OCCURRENCES 决定。
        """
        base = 600 + slot * 120
        for index in range(repeats):
            minutes_ago = base - index * 30
            self._search(self.user, source, 0, minutes_ago)
            self._search(self.user, target, 4, minutes_ago - 5)
        return SearchSynonymRejection.objects.create(
            source=source, target=target, reason=reason,
        )

    def _reject(self, source, target, reason='not_equivalent'):
        return SearchSynonymRejection.objects.create(source=source, target=target, reason=reason)

    def test_a_rejection_reports_how_often_the_rewrite_recurs(self):
        """否决必须带上出现次数，否则高频被否和一次性被否看起来一模一样。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)
        self._reject('充电宝', '移动电源')

        report = build_search_rejection_stats(days=7, now=self.now)

        row = report['rows'][0]
        self.assertEqual(row['occurrences'], 2)
        self.assertEqual(row['user_count'], 2)
        self.assertTrue(row['passes_threshold'])
        self.assertEqual(report['occurrence_total'], 2)

    def test_a_pair_that_never_recurs_is_reported_below_threshold(self):
        """周期内没再出现的词对要说清楚，而不是显示成一个零蛋。"""
        self._reject('台灯', '护眼灯')

        report = build_search_rejection_stats(days=7, now=self.now)

        row = report['rows'][0]
        self.assertEqual(row['occurrences'], 0)
        self.assertFalse(row['passes_threshold'])
        self.assertEqual(row['threshold_display'], '低于门槛')
        self.assertEqual(report['below_threshold_count'], 1)

    def test_a_dismissal_below_the_threshold_is_not_a_judgement_on_the_gate(self):
        """低于门槛的否决不进门槛判断：它说明不了门槛该松还是该紧。"""
        self._reject('台灯', '护眼灯', reason='too_rare')

        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(report['judged_count'], 0)
        self.assertEqual(report['signal']['kind'], 'insufficient')

    def test_a_handful_of_judged_rejections_is_not_a_conclusion(self):
        """一两条达到门槛的否决只是观感，不给方向。"""
        self._seed_judged_pair(
            '充电宝', '移动电源', 'too_rare', repeats=2,
        )

        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(report['judged_count'], 1)
        self.assertLess(report['judged_count'], MIN_JUDGED_REJECTIONS)
        self.assertEqual(report['signal']['kind'], 'insufficient')

    def test_a_pool_dismissed_as_not_equivalent_is_a_mining_problem(self):
        """高频词对仍被判为“不是同一个东西”，问题在挖掘规则而不在门槛。"""
        for index in range(MIN_JUDGED_REJECTIONS):
            self._seed_judged_pair(
                '错词%d' % index, '配错%d' % index, 'not_equivalent',
                repeats=3, slot=index,
            )

        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(report['signal']['kind'], 'misread')
        self.assertEqual(report['not_equivalent_count'], MIN_JUDGED_REJECTIONS)
        self.assertIn('挖掘规则', report['signal']['reason'])

    def test_a_pool_dismissed_as_too_rare_supports_tightening(self):
        """否决集中在“证据不足”时，才谈得上支持收紧门槛。"""
        for index in range(MIN_JUDGED_REJECTIONS):
            self._seed_judged_pair(
                '薄证%d' % index, '据不足%d' % index, 'too_rare',
                repeats=2, slot=index,
            )

        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(report['signal']['kind'], 'tighten')
        self.assertEqual(report['too_rare_count'], MIN_JUDGED_REJECTIONS)

    def test_mixed_reasons_give_no_single_direction(self):
        """两类理由都不占多数时维持现状，不硬凑一个方向。"""
        for index in range(MIN_JUDGED_REJECTIONS):
            self._seed_judged_pair(
                '混词%d' % index, '杂词%d' % index, 'ambiguous',
                repeats=2, slot=index,
            )

        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(report['signal']['kind'], 'hold')

    def test_the_reason_breakdown_counts_every_row(self):
        """原因分布要覆盖所有否决，包括低于门槛的那些。"""
        self._reject('甲', '乙', reason='not_equivalent')
        self._reject('丙', '丁', reason='too_rare')
        self._reject('戊', '己', reason='not_equivalent')

        report = build_search_rejection_stats(days=7, now=self.now)

        labels = {row['reason']: row['count'] for row in report['reason_rows']}
        self.assertEqual(labels['not_equivalent'], 2)
        self.assertEqual(labels['too_rare'], 1)
        self.assertEqual(sum(row['count'] for row in report['reason_rows']), 3)

    def test_the_module_never_writes_to_the_mining_threshold(self):
        """否决统计只读日志，绝不改候选次数门槛本身。"""
        import listings.search_rewrites as search_rewrites

        before = search_rewrites.MIN_OCCURRENCES
        for index in range(MIN_JUDGED_REJECTIONS):
            self._seed_judged_pair(
                '门槛%d' % index, '不动%d' % index, 'too_rare',
                repeats=3, slot=index,
            )

        build_search_rejection_stats(days=7, now=self.now)

        self.assertEqual(search_rewrites.MIN_OCCURRENCES, before)

    def test_an_empty_log_reports_nothing_rather_than_zeros(self):
        """一条否决都没有时不要摆出一堆零，直接说没有数据。"""
        report = build_search_rejection_stats(days=7, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertFalse(report['rows'])
        self.assertEqual(report['total_count'], 0)


class RejectionPanelTests(TestCase):
    """否决必须出现在运营真会看的那两个地方：页面与导出。"""

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='panel-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='panel-other', password='safe-password-123',
        )
        self.third_user = User.objects.create_user(
            username='panel-third', password='safe-password-123',
        )

    def _search(self, query, result_count, minutes_ago, user=None):
        record = SearchQuery.objects.create(
            user=user or self.user, query=query, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return record

    def _staff(self, username):
        return User.objects.create_superuser(
            username=username, email=f'{username}@example.com',
            password='safe-password-123',
        )

    def _seed_rejected_pair(self):
        self._search('充电宝', 0, 20)
        self._search('移动电源', 4, 10)
        self._search('充电宝', 0, 8)
        self._search('移动电源', 2, 5)
        SearchSynonymRejection.objects.create(
            source='充电宝', target='移动电源', reason='not_equivalent',
            note='两个是不同的类目',
        )

    def _seed_pending_pair(self):
        """再造一条还没被处理的候选。

        否决面板要同时有「已否决」和「待处理」两批词对：前者验证撤销
        入口，后者验证候选行内的否决按钮。把唯一的候选否掉之后列表就
        空了，按钮没有可挂靠的行。

        这条词对交给第三个用户，两次改口也独占一段时间。挖掘规则按同
        一用户的相邻记录串链，和前两条词对共用用户会让「充电宝」的下
        一条变成「台灯」，三条一起断掉。
        """
        self._search('台灯', 0, 90, user=self.third_user)
        self._search('护眼灯', 6, 80, user=self.third_user)
        self._search('台灯', 0, 70, user=self.third_user)
        self._search('护眼灯', 3, 60, user=self.third_user)

    def test_the_panel_appears_on_the_insights_page(self):
        self._seed_rejected_pair()
        self.client.force_login(self._staff('reject-view-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '候选否决分析')
        self.assertContains(response, '移动电源')

    def test_the_panel_explains_that_a_rejection_changes_no_search(self):
        """否决不改变搜索行为这件事必须写在页面上。"""
        self._seed_rejected_pair()
        self.client.force_login(self._staff('reject-note-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertContains(response, '不扩大也不缩小任何搜索的结果范围')

    def test_the_page_offers_a_reject_button_next_to_each_candidate(self):
        """候选行内就要能否决，否则运营要跳到后台才能干活。"""
        self._seed_rejected_pair()
        self._seed_pending_pair()
        self.client.force_login(self._staff('reject-action-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertContains(response, reverse('reject_rewrite_candidate'))
        self.assertContains(response, '>否决<')

    def test_the_candidate_panel_shows_the_rejection_count(self):
        """候选汇总里要能看到已经否掉多少条，否则运营不知道自己在进步。"""
        self._seed_rejected_pair()
        self.client.force_login(self._staff('reject-count-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertContains(response, '已被否决')

    def test_rejections_are_included_in_csv_export(self):
        """导出要能带走否决记录，运营才能把清单转给同事核对。"""
        self._seed_rejected_pair()
        self.client.force_login(self._staff('reject-export-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 7})

        self.assertEqual(export.status_code, 200)
        content = export.content.decode('utf-8-sig')
        self.assertIn('候选否决记录', content)
        self.assertIn('被否决词对', content)
        self.assertIn('移动电源', content)

    def test_the_export_separates_rejections_that_passed_the_gate(self):
        """导出必须区分达到门槛和低于门槛的否决，否则两个口径混在一起。"""
        self._seed_rejected_pair()
        SearchSynonymRejection.objects.create(
            source='台灯', target='护眼灯', reason='too_rare',
        )
        self.client.force_login(self._staff('reject-split-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 7})
        content = export.content.decode('utf-8-sig')

        self.assertIn('低于门槛', content)
        self.assertIn('达到门槛', content)

    def test_the_panel_offers_a_way_to_undo_a_rejection(self):
        """被否决词对旁边要有撤销入口，误判不能变成永久误判。"""
        self._seed_rejected_pair()
        self.client.force_login(self._staff('reject-undo-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertContains(response, reverse('restore_rewrite_candidate'))
        self.assertContains(response, '撤销否决')

    def test_the_panel_appears_with_no_rejections_at_all(self):
        """没有否决时面板要说清楚“还没有”，而不是留一块空白。"""
        self.client.force_login(self._staff('reject-empty-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '还没有记录过否决')
