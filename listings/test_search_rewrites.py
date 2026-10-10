from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import SearchQuery, SearchSynonym
from .search_rewrites import (
    MAX_REWRITE_GAP,
    build_search_rewrite_candidates,
)


class SearchRewriteCandidateTests(TestCase):
    """这些用例覆盖的是模块存在的理由：把用户真实的改口行为变成同义词候选。

    同义词此前完全靠运营手工猜测，漏掉的每一对都会让一个本来有货的搜索
    返回空结果。
    """

    def setUp(self):
        self.user = User.objects.create_user(username='rewrite-user', password='safe-password-123')
        self.other_user = User.objects.create_user(username='rewrite-other', password='safe-password-123')

    def _search(self, user, query, result_count, minutes_ago, **kwargs):
        """按相对时间写入搜索记录。

        SearchQuery.created_at 是 auto_now_add，创建时传值会被静默丢弃，
        必须先落库再用 update 写回，否则所有记录都堆在"现在"。
        """
        record = SearchQuery.objects.create(
            user=user, query=query, result_count=result_count, **kwargs,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return record

    def test_failed_then_successful_rewrite_by_one_user_is_a_candidate(self):
        """搜 A 无结果、紧接着搜 B 有结果，这是最典型的改口证据。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)

        report = build_search_rewrite_candidates(days=7)

        row = next(item for item in report['rows'] if item['source'] == '充电宝')
        self.assertEqual(row['target'], '移动电源')
        self.assertEqual(row['occurrences'], 2)
        self.assertEqual(row['user_count'], 2)
        self.assertTrue(report['has_data'])

    def test_two_different_users_do_not_form_a_pair(self):
        """两个不相关的人各搜各的，证明不了任何等价关系。"""
        self._search(self.user, '台灯', 0, 20)
        self._search(self.other_user, '雨伞', 5, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])
        self.assertFalse(report['has_data'])

    def test_rewrite_outside_time_gap_is_not_a_rewrite(self):
        """间隔太久就是新的需求，不是对旧需求的改写。"""
        self._search(self.user, '充电宝', 0, 300)
        self._search(self.user, '移动电源', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])
        self.assertEqual(MAX_REWRITE_GAP, timedelta(minutes=30))

    def test_successful_first_search_is_not_a_rewrite_signal(self):
        """第一次就搜到了，说明没有发生改口，不能算候选。"""
        self._search(self.user, '充电宝', 3, 20)
        self._search(self.user, '移动电源', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_failed_both_times_is_not_a_rewrite_signal(self):
        """两次都没结果只是换个词继续失败，没有证据表明两者等价。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 0, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_identical_terms_are_not_a_pair(self):
        """同一个词不算改写，否则每个重复搜索都会变成一条候选。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, ' 充电宝 ', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_existing_synonym_is_not_suggested_again(self):
        """运营已经配过的同义词不该反复出现在候选里。"""
        SearchSynonym.objects.create(keyword='充电宝', synonym='移动电源', is_active=True)
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_inactive_synonym_does_not_block_the_candidate(self):
        """停用的同义词不再影响搜索，候选应当重新出现。"""
        SearchSynonym.objects.create(keyword='充电宝', synonym='移动电源', is_active=False)
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)

        report = build_search_rewrite_candidates(days=7)

        self.assertEqual(len(report['rows']), 1)

    def test_single_occurrence_stays_below_the_threshold(self):
        """只出现一次是巧合，不该占用运营的注意力。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_anonymous_searches_are_ignored(self):
        """匿名搜索没有连续性，串起来会把别人的改口安到另一个人头上。"""
        self._search(None, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertFalse(report['rows'])

    def test_strongest_pair_comes_first(self):
        """候选按出现次数排序，运营应先看证据最足的那几条。"""
        # 同一用户的多次改口用递增分钟数区分，避免时间戳相同让排序退化
        for index in range(3):
            offset = 60 - index * 12
            self._search(self.user, '充电宝', 0, offset)
            self._search(self.user, '移动电源', 4, offset - 6)
        self._search(self.user, '台灯', 0, 22)
        self._search(self.user, '护眼灯', 4, 16)
        self._search(self.other_user, '台灯', 0, 20)
        self._search(self.other_user, '护眼灯', 4, 10)

        report = build_search_rewrite_candidates(days=7)

        self.assertEqual(report['rows'][0]['source'], '充电宝')
        self.assertEqual(report['rows'][0]['occurrences'], 3)
        self.assertEqual(report['summary']['strong_count'], 1)

    def test_candidates_are_only_proposed_never_applied(self):
        """模块只提议，绝不能自己改搜索结果。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)

        build_search_rewrite_candidates(days=7)

        self.assertFalse(SearchSynonym.objects.exists())

    def _make_staff(self, username):
        return User.objects.create_superuser(
            username=username, email=f'{username}@example.com', password='safe-password-123',
        )

    def _seed_candidate(self):
        """造出两条达到出现次数阈值的改口记录。"""
        self._search(self.user, '充电宝', 0, 20)
        self._search(self.user, '移动电源', 4, 10)
        self._search(self.other_user, '充电宝', 0, 8)
        self._search(self.other_user, '移动电源', 2, 5)

    def test_search_insights_renders_candidate_panel(self):
        """面板必须出现在搜索洞察页上，否则候选只是躺在数据库里的字符串。"""
        self._seed_candidate()
        self.client.force_login(self._make_staff('rewrite-view-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '搜索同义词候选')
        self.assertContains(response, '移动电源')

    def test_candidate_panel_links_to_admin_add_form_with_prefill(self):
        """一键确认要把两个词直接带进后台表单，多一步粘贴就没人愿意用。"""
        self._seed_candidate()
        self.client.force_login(self._make_staff('rewrite-link-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 7})

        expected = (
            reverse('admin:listings_searchsynonym_add')
            + '?keyword=%E5%85%85%E7%94%B5%E5%AE%9D&amp;synonym=%E7%A7%BB%E5%8A%A8%E7%94%B5%E6%BA%90'
        )
        self.assertContains(response, expected)

    def test_candidates_are_included_in_csv_export(self):
        """候选要能进导出，运营才能把清单转给同事批量确认。"""
        self._seed_candidate()
        self.client.force_login(self._make_staff('rewrite-export-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 7})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '同义词候选')
        self.assertContains(export, '需人工确认后启用')



class SearchSynonymFeedbackViewTests(TestCase):
    """效果回流必须出现在运营真会看的那两个地方：页面与导出。

    模块本身算得再对，如果只停留在数据库里，运营依然要在后台翻记录才能
    知道一条同义词究竟帮上了忙还是帮了倒忙——那这个闭环等于没闭上。
    """

    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(username='feedback-user', password='safe-password-123')

    def _search(self, term, days_ago, result_count):
        """按距确认时刻的相对天数写入搜索记录。"""
        record = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=record.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return record

    def _make_confirmed_synonym(self, keyword, synonym, age_days=30):
        """造一条已经确认满一个观察期的同义词。"""
        row = SearchSynonym.objects.create(keyword=keyword, synonym=synonym)
        SearchSynonym.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(days=age_days),
        )
        return row

    def _seed_improving_pair(self):
        """确认前全是无结果搜索，确认后都能搜到东西。"""
        self._make_confirmed_synonym('台灯', '护眼灯')
        for offset in range(3):
            self._search('台灯', 42 - offset, 0)
        for offset in range(3):
            self._search('台灯', 28 - offset, 4)

    def _staff(self, username):
        return User.objects.create_superuser(
            username=username, email=f'{username}@example.com', password='safe-password-123',
        )

    def test_feedback_panel_appears_on_the_insights_page(self):
        """页面要有效果回流面板，否则运营看不到闭环。"""
        self._seed_improving_pair()
        self.client.force_login(self._staff('feedback-view-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 180})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '同义词效果回流')
        self.assertContains(response, '无结果率下降')
        self.assertContains(response, '台灯')

    def test_feedback_panel_explains_the_disabled_exclusion(self):
        """停用不参与统计这件事必须写在页面上，否则运营会以为数据漏了。"""
        row = self._make_confirmed_synonym('台灯', '护眼灯', age_days=30)
        row.is_active = False
        row.save()
        self.client.force_login(self._staff('feedback-disabled-staff'))

        response = self.client.get(reverse('search_insights'), {'days': 180})

        self.assertContains(response, '已停用的同义词不参与统计')

    def test_feedback_rows_are_included_in_csv_export(self):
        """导出要带回流列，运营才能把结论带走逐条处理。"""
        self._seed_improving_pair()
        self.client.force_login(self._staff('feedback-export-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 180})

        self.assertEqual(export.status_code, 200)
        self.assertContains(export, '同义词效果回流')
        self.assertContains(export, '观察无结果率')
        self.assertContains(export, '台灯')

    def test_feedback_rows_come_before_the_candidate_section(self):
        """导出里回流段排在候选段之前：回流是对已确认同义词的验收，早就排在待确认的候选前面，运营会先看结论。"""
        self._seed_improving_pair()
        self.client.force_login(self._staff('feedback-order-staff'))

        export = self.client.get(reverse('search_insights_export'), {'days': 180})

        content = export.content.decode('utf-8-sig')
        # 段落标题本身就是一行，按行首匹配才不会被正文里提到这两个词的地方干扰
        section_lines = [
            index for index, line in enumerate(content.splitlines())
            if line.startswith('同义词效果回流') or line.startswith('同义词候选')
        ]
        self.assertEqual(len(section_lines), 2)
        # 第一个匹配到的就是回流段，它应该出现在候选段之前
        feedback_line, candidate_line = section_lines
        self.assertLess(feedback_line, candidate_line)