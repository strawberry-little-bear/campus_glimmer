# -*- coding: utf-8 -*-
"""看否决后来还在不在被人认着，以及那个词对有没有自己回来。

这些用例守住的不是算术，而是四条边界。

撤销和覆盖必须分开报。一条否决被确认覆盖和一个被撤销删除，说的都是
「运营推翻了自己」，但推反的方向相反：一个是「当初否错了，它其实是个等价
关系」，一个是「当初否错了，它不该被挡」。合成一个「撤销率」会把两个相反
的修正搅成一个数字，而那个数字什么都说明不了。

回头次数不能跨失效累加。一条已被覆盖的否决不再挡任何候选，它之后的改口
属于一个已经在扩大搜索的词对，不属于一条仍在生效的判断。把那段算进去，
一个早就不作数的否决会背上一个它管不着的词对的账。

回头必须用和挖掘同一条规则数。两把尺子量出来的「否决前 8 次」和「否决后
12 次」摆在一张表里，读者没有任何办法知道它们不可比。

被撤销的否决读不到，就必须说读不到。撤销只写消息日志、不落表，硬编一个
0 出来，等于让一个看不见的桶看起来真的没有事。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import SearchQuery, SearchSynonym, SearchSynonymRejection
from .search_rejection_recurrence import (
    MIN_JUDGED_REJECTIONS,
    MIN_RECURRENCES,
    build_search_rejection_recurrence,
)


class RejectionRecurrenceTests(TestCase):
    """否决的回头与被推翻。"""

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='recur-user', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='recur-other', password='safe-password-123',
        )
        self.staff = User.objects.create_superuser(
            username='recur-staff', email='recur-staff@example.com',
            password='safe-password-123',
        )

    def _search(self, user, query, result_count, minutes_ago):
        """按相对分钟数写入搜索记录。

        `SearchQuery.created_at` 是 auto_now_add，创建时传值会被静默丢弃，
        必须先落库再 update 写回，否则所有记录都堆在「现在」。
        """
        record = SearchQuery.objects.create(
            user=user, query=query, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=self.now - timedelta(minutes=minutes_ago),
        )
        return record

    def _reject(self, source='充电宝', target='移动电源', reason='not_equivalent',
                hours_ago=48):
        """造一条几小时前的否决，时间可调是为了让回头区间有处可切。"""
        row = SearchSynonymRejection.objects.create(
            source=source, target=target, reason=reason, rejected_by=self.staff,
        )
        SearchSynonymRejection.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(hours=hours_ago),
        )
        return row

    def _confirm(self, source='充电宝', target='移动电源'):
        """确认同义词并按既有入口把否决标记为失效。"""
        from .search_rejection_stats import supersede_rejections_for_pair

        SearchSynonym.objects.create(keyword=source, synonym=target)
        return supersede_rejections_for_pair(source, target)

    # --- 回头次数 -------------------------------------------------------

    def test_a_pair_that_comes_back_after_being_dismissed_is_counted(self):
        """否决之后这个词对又出现，就必须被数出来，否则否决是一次删除。"""
        # 否决之前的那次改口是运营做判断时看到的证据，不算回头。
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(hours_ago=6)
        # 否决之后三次改口：两次同一个用户、一次另一个用户。
        self._search(self.user, '充电宝', 0, 200)
        self._search(self.user, '移动电源', 5, 190)
        self._search(self.other_user, '充电宝', 0, 150)
        self._search(self.other_user, '移动电源', 6, 140)
        self._search(self.user, '充电宝', 0, 100)
        self._search(self.user, '移动电源', 7, 90)

        report = build_search_rejection_recurrence(now=self.now)
        row = report['rows'][0]

        self.assertEqual(row['recurrences'], 3)
        self.assertTrue(row['has_recurred'])
        self.assertEqual(report['recurrence_total'], 3)
        self.assertEqual(report['recurred_count'], 1)

    def test_occurrences_before_the_dismissal_are_not_its_recurrence(self):
        """否决之前的改口是当初判它的证据，不是它后来的回头。

        把否决前的账算进来，一条从没回来过的否决也会显示「回头 4 次」——
        而那 4 次正是运营做判断时看到的东西。
        """
        for index in range(4):
            self._search(self.user, '充电宝', 0, 600 - index * 10)
            self._search(self.user, '移动电源', 4, 595 - index * 10)
        self._reject(hours_ago=1)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['recurrence_total'], 0)
        self.assertEqual(report['recurred_count'], 0)
        self.assertFalse(report['rows'][0]['has_recurred'])

    def test_recurrences_do_not_accrue_after_the_dismissal_is_superseded(self):
        """失效之后的改口不属于一条仍在生效的判断。

        被覆盖的否决已经不再挡任何候选，那段时间的改口属于一个正在扩大搜索
        的词对。把它算进来，一个早就不作数的否决会背上一个它管不着的账。
        """
        self._reject(hours_ago=48)
        self._confirm()
        # 把失效时刻压到 12 小时前，下面两次改口就都落在失效之后了。
        SearchSynonymRejection.objects.filter(
            source='充电宝', target='移动电源',
        ).update(superseded_at=self.now - timedelta(hours=12))
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)

        report = build_search_rejection_recurrence(now=self.now)
        row = report['rows'][0]

        self.assertEqual(row['kind'], 'superseded')
        self.assertTrue(row['is_synonym_now'])
        self.assertEqual(row['recurrences'], 0)
        self.assertEqual(report['recurrence_total'], 0)

    def test_a_quiet_pair_reports_zero_rather_than_none(self):
        """否决之后再没出现过的词对，回头是 0 而不是「不知道」。

        回头数的是「生效期间出现了多少次」，窗口内一次都没有就是 0 次，这是
        一个实数；和「本期没有任何提醒」那种分母不存在的 None 不是一回事。
        """
        self._search(self.user, '台灯', 0, 600)
        self._search(self.user, '台灯', 4, 590)
        self._reject(source='台灯', target='照明', hours_ago=48)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['rows'][0]['recurrences'], 0)
        self.assertFalse(report['rows'][0]['has_recurred'])
        self.assertEqual(report['summary'].count('回头'), 0)

    def test_recurrences_use_the_same_rule_as_the_miner(self):
        """回头的数必须和候选面板对得上，否则两张表不可比。

        判据是「同一个人、间隔不超过 MAX_REWRITE_GAP、前一次无结果、后一次
        有结果」。这里刻意造一个间隔超过上限的改口，它两边都不该算。
        """
        self._reject(hours_ago=48)
        # 间隔 90 分钟，超过 30 分钟的改口链上限，两边的挖掘都不认它。两次都
        # 造在否决之后：判据是「间隔超限」，不能顺手把否决前的证据混进来。
        self._search(self.user, '充电宝', 0, 200)
        self._search(self.user, '移动电源', 4, 110)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['recurrence_total'], 0)

    # --- 推翻 -----------------------------------------------------------

    def test_a_standing_dismissal_and_a_superseded_one_are_counted_apart(self):
        """仍然生效和被确认覆盖必须分开报。

        两者都是「否决留下的痕迹」，但一个说明判断还作数，一个说明判断被自
        己推翻了。合成一个数，读者无法区分「很稳」和「一半否错了」。
        """
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(hours_ago=48)
        self._search(self.user, '键盘', 0, 500)
        self._search(self.user, '键鼠', 4, 490)
        self._reject(source='键盘', target='键鼠', hours_ago=48)
        self._confirm()

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['total_count'], 2)
        self.assertEqual(report['standing_count'], 1)
        self.assertEqual(report['superseded_count'], 1)
        self.assertEqual(report['reversed_count'], 1)

    def test_a_superseded_dismissal_is_not_an_error(self):
        """覆盖是机制在正常工作，不该被写成一条警告。

        运营在更充分的证据下改变主意，正是这套挖掘—确认—否决闭环该有的样
        子。把它报成异常，等于惩罚运营使用自己的判断。
        """
        for index in range(5):
            source, target = f'词{index}', f'物{index}'
            self._search(self.user, source, 0, 600 - index)
            self._search(self.user, target, 4, 590 - index)
            self._reject(source=source, target=target, hours_ago=48)
        # 五条里只有一条被确认覆盖：够得上样本线，也够不上「该停下来」。
        self._confirm(source='词0', target='物0')

        report = build_search_rejection_recurrence(now=self.now)

        self.assertTrue(report['is_measurable'])
        self.assertFalse(report['is_reversed'])
        self.assertIn('判断整体是稳的', report['summary'])

    def test_a_high_reversal_rate_is_flagged_as_a_design_question(self):
        """推翻率偏高时报出来，这是产品问题不是运营问题。"""
        for index in range(6):
            source, target = f'词{index}', f'物{index}'
            self._search(self.user, source, 0, 600 - index)
            self._search(self.user, target, 4, 590 - index)
            self._reject(source=source, target=target, hours_ago=6)
        for index in range(3):
            self._confirm(source=f'词{index}', target=f'物{index}')

        report = build_search_rejection_recurrence(now=self.now)

        self.assertTrue(report['is_measurable'])
        self.assertEqual(report['superseded_count'], 3)
        self.assertEqual(report['reversed_share'], 50.0)
        self.assertTrue(report['is_reversed'])
        self.assertIn('值得回头看一眼', report['summary'])

    def test_too_few_dismissals_say_so_instead_of_reporting_a_share(self):
        """样本不够时不报推翻率。

        五条以下，一条被覆盖就是 20% 的推翻率——一个百分号和一个百分号加一
        个百分号不是同一个量级的东西。
        """
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(hours_ago=6)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertFalse(report['is_measurable'])
        self.assertIsNone(report['reversed_share'])
        self.assertFalse(report['is_reversed'])
        self.assertIn(
            f'样本不足 {MIN_JUDGED_REJECTIONS} 条', report['summary'],
        )

    def test_an_undone_dismissal_is_not_invented_as_zero(self):
        """撤销只写消息日志不落表，本模块不为它编一个数。

        硬报一个「已撤销 0 条」，等于让一个看不见的桶看起来真的没有事；读者
        也没有办法把这个 0 和「一条都没撤销」区分开。
        """
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        row = self._reject(hours_ago=6)
        row.delete()

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['total_count'], 0)
        self.assertFalse(report['has_data'])
        self.assertIn('还没有否决记录', report['summary'])

    # --- 窗口与边界 -----------------------------------------------------

    def test_an_older_dismissal_falls_outside_the_window(self):
        """窗口外的否决不参与统计，两个面板描述的是同一段日志。"""
        self._reject(hours_ago=24 * 200)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['total_count'], 0)

    def test_rows_put_the_actionable_recurrence_first(self):
        """会自己回来的排最前，那是唯一一条运营现在还能有所动作的线索。"""
        # 两条否决各自的证据都在否决之前，不参与回头。
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(source='充电宝', target='移动电源', hours_ago=6)
        self._search(self.user, '键盘', 0, 600)
        self._search(self.user, '键鼠', 4, 590)
        self._reject(source='键盘', target='键鼠', hours_ago=6)
        # 否决之后两条都回头 3 次；差别只在一条仍然生效、一条已被确认覆盖。
        # 两段时间错开，否则两条链会互相穿插，改口记到第三个词对上去。
        for minutes in (200, 150, 100):
            self._search(self.user, '充电宝', 0, minutes)
            self._search(self.user, '移动电源', 5, minutes - 10)
        for minutes in (90, 60, 30):
            self._search(self.user, '键盘', 0, minutes)
            self._search(self.user, '键鼠', 5, minutes - 10)
        self._confirm(source='键盘', target='键鼠')

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['rows'][0]['source'], '充电宝')
        self.assertEqual(report['rows'][0]['recurrences'], 3)
        self.assertTrue(report['rows'][0]['has_recurred'])
        self.assertEqual(report['rows'][1]['source'], '键盘')
        self.assertEqual(report['rows'][1]['kind'], 'superseded')

    def test_a_thin_recurrence_is_not_called_a_comeback(self):
        """一两次回头不构成「回来了」，稀疏事件上少即是噪声。"""
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(hours_ago=6)
        self._search(self.user, '充电宝', 0, 200)
        self._search(self.user, '移动电源', 4, 190)

        report = build_search_rejection_recurrence(now=self.now)

        self.assertEqual(report['rows'][0]['recurrences'], 1)
        self.assertFalse(report['rows'][0]['has_recurred'])
        self.assertEqual(report['recurred_count'], 0)

    def test_the_reason_travels_with_the_row(self):
        """否决原因要跟着行走，回头和原因要能对着读。"""
        self._search(self.user, '充电宝', 0, 600)
        self._search(self.user, '移动电源', 4, 590)
        self._reject(reason='too_rare', hours_ago=6)
        self._search(self.user, '充电宝', 0, 200)
        self._search(self.user, '移动电源', 4, 190)

        report = build_search_rejection_recurrence(now=self.now)
        row = report['rows'][0]

        self.assertEqual(row['reason'], 'too_rare')
        self.assertEqual(row['reason_label'], '出现次数太少，证据不足')
        self.assertEqual(row['recurrences'], 1)
        self.assertGreaterEqual(MIN_RECURRENCES, 3)

    def test_an_empty_window_says_there_is_nothing_to_read(self):
        """一条否决都没有时直说，不报 0% 的推翻率。"""
        report = build_search_rejection_recurrence(now=self.now)

        self.assertFalse(report['has_data'])
        self.assertIsNone(report['reversed_share'])
        self.assertFalse(report['is_measurable'])
        self.assertFalse(report['is_reversed'])
        self.assertEqual(report['rows'], [])
