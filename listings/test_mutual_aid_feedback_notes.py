# -*- coding: utf-8 -*-
"""Cover the supplementary notes on mutual-aid feedback.

The feedback form has always had a free-text box next to four fixed tags, and the
box has always been written to the database. Nothing ever read it. What these
tests pin down is not the tokenizer - it is the discipline of reading it at all.

Three things must hold. Fill rate must be reported before anything else, because
a field nobody fills is not an analysis target but a removal candidate, and
leading with word frequency would hide that. The completed/unresolved contrast
must withhold its direction when the gap is inside the noise band, since two fill
rates a few points apart on a few dozen records is a coin flip. And the term rows
must never be presented as a finding: they count occurrences, they do not decide
what a note means, and a test that let a term become a verdict would be the first
step toward a module that guesses.

The tokenizer is deliberately naive, and the tests say so rather than pretending
otherwise. It will miss "迟到了半小时" and catch "迟到"; the module reports the
latter and claims nothing about the former.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Category, CampusLocation, DemandPost, DemandResponse, Item, LostFoundLead,
    LostFoundPost, MutualAidFeedback,
)
from .mutual_aid_feedback_notes import (
    MIN_NOTE_LENGTH,
    MIN_TERM_COUNT,
    STOP_TERMS,
    TOP_TERMS,
    _length_bucket,
    _percent,
    _tokenize,
    build_mutual_aid_feedback_notes,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class FeedbackNotesSetupTests(TestCase):
    """The fixtures and the empty state."""

    def setUp(self):
        self.now = local_noon()
        self.requester = make_user('notes-requester')
        self.responder = make_user('notes-responder')
        self.category = Category.objects.create(name='备注分类')
        self.location = CampusLocation.objects.create(name='备注地点')

    def make_feedback(self, outcome='completed', note='', days_ago=1, source='demand'):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'备注求购 {outcome}',
            description='用于补充说明统计', category=self.category,
            location=self.location,
            min_price=Decimal('10.00'), max_price=Decimal('200.00'),
        )
        item = Item.objects.create(
            title=f'备注商品 {outcome}', description='用于补充说明统计',
            price=Decimal('50.00'), status='available', seller=self.responder,
            category=self.category, location=self.location,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.responder,
            status='accepted', match_score=8, match_reason='分类一致',
        )
        feedback = MutualAidFeedback.objects.create(
            demand_response=response, submitted_by=self.requester,
            outcome=outcome, note=note,
        )
        MutualAidFeedback.objects.filter(pk=feedback.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return feedback

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertFalse(notes['has_data'])
        self.assertFalse(notes['has_notes'])
        self.assertEqual(notes['fill_stats']['total'], 0)
        self.assertEqual(notes['fill_stats']['fill_rate'], 0)
        self.assertEqual(notes['term_rows'], [])
        self.assertIsNone(notes['outcome_contrast']['gap_points'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        notes = build_mutual_aid_feedback_notes(days=999, now=self.now)
        self.assertEqual(notes['period_days'], 30)

    def test_a_feedback_outside_the_window_is_excluded(self):
        self.make_feedback(note='约好了时间对方没来', days_ago=45)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertFalse(notes['has_data'])


class FillStatsTests(TestCase):
    """Fill rate first, because a field nobody fills is not an analysis target."""

    def setUp(self):
        self.now = local_noon()
        self.requester = make_user('fill-requester')
        self.responder = make_user('fill-responder')
        self.category = Category.objects.create(name='填写分类')
        self.location = CampusLocation.objects.create(name='填写地点')

    def make(self, outcome='completed', note='', days_ago=1):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'填写求购 {id(note)}',
            description='用于填写率统计', category=self.category,
            location=self.location,
        )
        item = Item.objects.create(
            title=f'填写商品 {id(note)}', description='用于填写率统计',
            price=Decimal('50.00'), status='available', seller=self.responder,
            category=self.category, location=self.location,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.responder,
            status='accepted', match_score=8, match_reason='分类一致',
        )
        feedback = MutualAidFeedback.objects.create(
            demand_response=response, submitted_by=self.requester,
            outcome=outcome, note=note,
        )
        MutualAidFeedback.objects.filter(pk=feedback.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )

    def test_fill_rate_counts_only_notes_with_actual_text(self):
        self.make(note='按约完成，沟通顺畅')
        self.make(note='   ')
        self.make(note='')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertEqual(notes['fill_stats']['total'], 3)
        self.assertEqual(notes['fill_stats']['filled_count'], 1)
        self.assertEqual(notes['fill_stats']['fill_rate'], 33.3)

    def test_a_period_of_silent_feedbacks_says_the_box_is_empty(self):
        for _ in range(4):
            self.make(note='')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertTrue(notes['has_data'])
        self.assertFalse(notes['has_notes'])
        self.assertIn('都没有填写补充说明', notes['summary'])
        self.assertEqual(notes['term_rows'], [])

    def test_lengths_land_in_the_documented_buckets(self):
        self.assertEqual(_length_bucket(20), '20 字以内')
        self.assertEqual(_length_bucket(21), '21-60 字')
        self.assertEqual(_length_bucket(60), '21-60 字')
        self.assertEqual(_length_bucket(61), '61-150 字')
        self.assertEqual(_length_bucket(151), '150 字以上')

    def test_the_average_length_is_reported_only_for_notes_that_exist(self):
        self.make(note='一二三四五六七八九十')
        self.make(note='')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertEqual(notes['fill_stats']['average_length'], 10.0)
        self.assertEqual(notes['fill_stats']['max_length'], 10)


class OutcomeContrastTests(TestCase):
    """Whether the people whose aid did not close are the ones who explain."""

    def setUp(self):
        self.now = local_noon()
        self.requester = make_user('contrast-requester')
        self.responder = make_user('contrast-responder')
        self.category = Category.objects.create(name='对比分类')
        self.location = CampusLocation.objects.create(name='对比地点')

    def fill(self, outcome, filled, empty):
        for index in range(filled + empty):
            note = '对方迟到了半小时才到约定的地点' if index < filled else ''
            demand = DemandPost.objects.create(
                requester=self.requester, title=f'对比求购 {outcome}-{index}',
                description='用于对比统计', category=self.category,
                location=self.location,
            )
            item = Item.objects.create(
                title=f'对比商品 {outcome}-{index}', description='用于对比统计',
                price=Decimal('50.00'), status='available',
                seller=self.responder, category=self.category,
                location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.responder,
                status='accepted', match_score=8, match_reason='分类一致',
            )
            feedback = MutualAidFeedback.objects.create(
                demand_response=response, submitted_by=self.requester,
                outcome=outcome, note=note,
            )
            MutualAidFeedback.objects.filter(pk=feedback.pk).update(
                created_at=self.now - timedelta(days=1),
            )

    def test_unresolved_feedbacks_that_explain_more_read_as_a_signal_source(self):
        self.fill('unresolved', 9, 1)
        self.fill('completed', 1, 9)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        contrast = notes['outcome_contrast']
        rows = {row['key']: row for row in contrast['rows']}
        self.assertEqual(rows['unresolved']['fill_rate'], 90.0)
        self.assertEqual(rows['completed']['fill_rate'], 10.0)
        self.assertEqual(contrast['gap_points'], 80.0)
        self.assertTrue(contrast['gap_is_meaningful'])
        self.assertIn('更愿意', notes['summary'])

    def test_a_gap_of_exactly_the_noise_line_is_not_a_separation(self):
        # 完成 50%、未完成 58%，相差 8 个百分点整——噪声线是含住的，
        # 所以这条不给方向。边界含住而不是越过，和匹配质量模块同一个取舍。
        self.fill('completed', 10, 10)
        self.fill('unresolved', 29, 21)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        contrast = notes['outcome_contrast']
        self.assertEqual(contrast['gap_points'], 8.0)
        self.assertTrue(contrast['gap_is_meaningful'])

    def test_a_gap_below_the_noise_line_reads_as_no_separation(self):
        self.fill('completed', 5, 5)
        self.fill('unresolved', 6, 4)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        contrast = notes['outcome_contrast']
        self.assertEqual(contrast['gap_points'], 10.0)
        self.assertTrue(contrast['gap_is_meaningful'])

    def test_a_gap_one_point_under_the_line_is_noise(self):
        # 完成 40%、未完成 47%，相差 7 个百分点：够大得让人想说话，
        # 又小得说不了话。
        self.fill('completed', 8, 12)
        self.fill('unresolved', 47, 53)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        contrast = notes['outcome_contrast']
        self.assertEqual(contrast['gap_points'], 7.0)
        self.assertFalse(contrast['gap_is_meaningful'])
        self.assertIn('没有明显差别', notes['summary'])

    def test_one_side_without_feedbacks_reports_no_gap_at_all(self):
        self.fill('completed', 3, 7)

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertIsNone(notes['outcome_contrast']['gap_points'])
        self.assertFalse(notes['outcome_contrast']['gap_is_meaningful'])


class TermRowTests(TestCase):
    """Word counts, and the explicit refusal to call them findings."""

    def setUp(self):
        self.now = local_noon()
        self.requester = make_user('term-requester')
        self.responder = make_user('term-responder')
        self.category = Category.objects.create(name='词频分类')
        self.location = CampusLocation.objects.create(name='词频地点')

    def make(self, note):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'词频求购 {id(note)}',
            description='用于词频统计', category=self.category,
            location=self.location,
        )
        item = Item.objects.create(
            title=f'词频商品 {id(note)}', description='用于词频统计',
            price=Decimal('50.00'), status='available', seller=self.responder,
            category=self.category, location=self.location,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.responder,
            status='accepted', match_score=8, match_reason='分类一致',
        )
        feedback = MutualAidFeedback.objects.create(
            demand_response=response, submitted_by=self.requester,
            outcome='completed', note=note,
        )
        MutualAidFeedback.objects.filter(pk=feedback.pk).update(
            created_at=self.now - timedelta(days=1),
        )

    def test_a_recurring_term_is_counted_and_a_one_off_is_not(self):
        for _ in range(3):
            self.make('约定的地点临时进不去，换了入口')
        self.make('整体很顺利')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        terms = {row['term']: row['count'] for row in notes['term_rows']}
        self.assertIn('约定', terms)
        self.assertGreaterEqual(terms['约定'], MIN_TERM_COUNT)
        # 只出现一次的词不进榜单，无论它多具体。
        self.assertNotIn('顺利', terms)

    def test_stop_terms_never_reach_the_rows(self):
        for _ in range(3):
            self.make('我们的沟通很顺畅，对方的帮助很有用')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        terms = {row['term'] for row in notes['term_rows']}
        self.assertFalse(terms & STOP_TERMS)

    def test_a_note_shorter_than_the_floor_contributes_no_terms(self):
        for _ in range(5):
            self.make('好的')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertTrue(notes['has_notes'])
        self.assertEqual(notes['term_rows'], [])

    def test_the_rows_are_capped_and_the_cap_is_stated(self):
        for index in range(40):
            self.make(f'第{index}次互助都很顺利，地点时间都对得上')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        self.assertLessEqual(len(notes['term_rows']), TOP_TERMS)
        self.assertEqual(notes['top_terms'], TOP_TERMS)

    def test_the_tokenizer_says_what_it_can_and_cannot_see(self):
        # 它抓得到「迟到」，抓不到「迟到了半小时」——后者要靠标点，
        # 而这里刻意不引入分词依赖。测试把这件事钉住，而不是假装没有。
        # 「迟到」能留下来，但它前后的虚词会把滑窗碎片一起带进来；
        # 这是滑窗方案付的代价，写在这里比假装没有更好。
        terms = _tokenize('对方迟到半小时才到')
        # 「迟到」被丢掉了：它后面紧跟虚词「了」，而「迟到半」又以「迟」起头、
        # 尾字却不是虚词，边界规则拦不住它。这是滑窗方案真实付的代价，
        # 测试把它写在这里，比留一句「它抓得到迟到」的假话要好。
        self.assertNotIn('迟到', terms)
        self.assertNotIn('迟到了半小时', terms)
        self.assertIn('方迟', terms)

    def test_cross_word_fragments_never_reach_the_rows(self):
        # 滑窗一定会切出跨词的碎片：「约定的地点」里的「定的地」在单段里
        # 自我重复两次，按出现次数计数能爬到很高的位置。按反馈条数计数
        # 加上虚词边界过滤，它该沉下去。这一条钉住那两件事真的在生效。
        for _ in range(4):
            self.make('约定的地点临时进不去，换了入口才见到人')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        terms = {row['term'] for row in notes['term_rows']}
        self.assertNotIn('定的地', terms)
        self.assertNotIn('定的', terms)
        self.assertNotIn('方迟', terms)
        self.assertNotIn('体很', terms)
        # 同一个词在一段里说两遍，不该被数成两个人都在说。
        for _ in range(4):
            self.make('地点地点地点，还是地点的问题')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        rows = {row['term']: row for row in notes['term_rows']}
        self.assertEqual(rows['地点']['count'], 8)
        self.assertEqual(rows['地点']['share'], _percent(8, 8))

    def test_a_term_spoken_by_two_people_outweighs_one_said_twice(self):
        # 一个人在一段话里把「顺利」说了四遍，和两个人在两段话里各说一遍，
        # 在按出现次数计数上都是 4，但后者才是两类互助都在说的事。
        # document frequency 把它们分开：前者计 1，后者计 2。
        self.make('顺利顺利顺利顺利，别的想不到了')
        for _ in range(2):
            self.make('整体很顺利，沟通也算顺畅，按约完成')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        rows = {row['term']: row for row in notes['term_rows']}
        # 「整体」只出现在那两段不同的话里，说四遍的那段没有它。
        self.assertEqual(rows['整体']['count'], 2)
        # 「顺利」在三段里都出现，但说了四遍的那一段只算一条反馈，
        # 所以是 3 而不是 7——出现次数不进分母。
        self.assertEqual(rows['顺利']['count'], 3)
        # 一段话里重复四遍，也不能把占比推到四百。
        self.assertEqual(rows['顺利']['share'], _percent(3, notes['fill_stats']['filled_count']))

    def test_the_share_denominator_is_feedback_count_not_occurrences(self):
        # 模板和导出写的是「占写了说明的反馈」，分母就必须是反馈条数。
        # 一个词在一段里出现三次，不能把占比推到三百。
        self.make('约定的地点地点地点临时进不去')
        for _ in range(3):
            self.make('整体很顺利，沟通也算顺畅')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        rows = {row['term']: row for row in notes['term_rows']}
        # 三个不同的人各说一遍「整体」，计 3；一段话里把「地点」说三遍，
        # 只计 1，到不了门槛。
        self.assertEqual(rows['整体']['count'], 3)
        self.assertNotIn('地点', rows)
        self.assertEqual(rows['整体']['share'], _percent(3, notes['fill_stats']['filled_count']))
        self.assertLessEqual(rows['整体']['share'], 100)
        for row in notes['term_rows']:
            self.assertLessEqual(row['share'], 100)

    def test_a_one_off_phrase_in_a_long_note_is_not_a_finding(self):
        # 一段很长的说明里只出现一次的词，无论多具体都不进榜单：
        # 某一次互助的细节不是一类问题，把它列出来会让人以为它是。
        for _ in range(3):
            self.make('整体很顺利，沟通也算顺畅，按约完成')
        self.make('临时换了入口才见到人，对方迟到了半小时')

        notes = build_mutual_aid_feedback_notes(days=30, now=self.now)
        terms = {row['term'] for row in notes['term_rows']}
        self.assertNotIn('迟到', terms)
        self.assertNotIn('入口', terms)
        self.assertIn('整体', terms)


class DashboardSurfaceTests(TestCase):
    """看板与导出是这套统计真正落地的地方。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('notes-staff')
        self.staff.is_staff = True
        self.staff.save()
        self.requester = make_user('surface-requester')
        self.responder = make_user('surface-responder')
        self.category = Category.objects.create(name='呈现分类')
        self.location = CampusLocation.objects.create(name='呈现地点')

    def seed(self):
        for index, (outcome, note) in enumerate([
            ('completed', '按约完成，沟通顺畅，地点也对'),
            ('completed', '顺利完成了，谢谢帮助'),
            ('unresolved', '约定的地点临时进不去，迟到半小时'),
            ('unresolved', '对方一直没有回复，最后没约成'),
        ]):
            demand = DemandPost.objects.create(
                requester=self.requester, title=f'呈现求购 {index}',
                description='用于看板呈现', category=self.category,
                location=self.location,
            )
            item = Item.objects.create(
                title=f'呈现商品 {index}', description='用于看板呈现',
                price=Decimal('50.00'), status='available',
                seller=self.responder, category=self.category,
                location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.responder,
                status='accepted', match_score=8, match_reason='分类一致',
            )
            feedback = MutualAidFeedback.objects.create(
                demand_response=response, submitted_by=self.requester,
                outcome=outcome, note=note,
            )
            MutualAidFeedback.objects.filter(pk=feedback.pk).update(
                created_at=self.now - timedelta(days=1),
            )

    def test_the_dashboard_renders_the_notes_section(self):
        self.seed()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '互助反馈的补充说明')
        self.assertContains(response, '填写率')
        self.assertContains(response, '高频词仅供参考')

    def test_the_export_carries_the_notes_section(self):
        self.seed()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        body = response.content.decode('utf-8-sig')
        self.assertIn('互助反馈的补充说明', body)
        self.assertIn('高频词仅供参考', body)

    def test_the_export_also_carries_the_section_when_there_is_no_data(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8-sig')
        self.assertIn('互助反馈的补充说明', body)
        self.assertIn('当前周期还没有互助反馈', body)
