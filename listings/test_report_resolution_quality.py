# -*- coding: utf-8 -*-
"""为「商品举报处理得公不公平」钉住口径。

`Report` 是站内最早的治理队列，读路径只有 `governance_sla` 把它并进治理队列比时效、
`analytics` 报新增与待处理的计数。这个模块把它读出来，只报告不改写。这些测试要钉住的
是：

1. 驳回率按 `reason` 拆开，不按总数报——四种举报原因的举证难度完全不是一回事。
2. 已完结的举报才算进驳回率，`pending` / `reviewing` 是右删失样本，单独数出规模。
3. 审核意见只报「写了没写」和长度，一个字符内容都不读；按结局拆填写率。
4. 不把驳回率和处理人挂钩——不报 `reviewer` 的任何拆分。
5. 集中度只报规模与形状，不列商品名、不列卖家名。
6. 举报人只报分布形状，不排名、不报任何人的用户名。
7. 不报「同一用户对同一商品的重复举报」——`UniqueConstraint` 让那个数恒为 0。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Category, Item, Report
from .report_resolution_quality import (
    MIN_BUCKET_SAMPLE, NOISE_POINTS,
    _band_index, _note_length_index,
    build_report_resolution_quality,
)


def local_tonight():
    """今天 23:59，一个确保晚于测试运行时刻的参照点。

    举报的 ``created_at`` 是 ``auto_now_add``，写的是真实当前时间。参照点必须晚于它，
    否则周期内的举报会被 ``created_at__lte=now`` 整批排除——用「今天中午」在下午跑测试
    就会踩到这个坑，而它长得像一个计算错误。
    """
    return timezone.localtime().replace(hour=23, minute=59, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_report(item, reporter, **kwargs):
    """建一条举报，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update
    写回。否则所有举报都落在「现在」，统计窗口读不出来。
    """
    report = Report.objects.create(
        item=item, reporter=reporter,
        reason=kwargs.pop('reason', 'scam'),
    )
    fields = {}
    for name in ('created_at', 'reviewed_at'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    for name in ('status', 'review_note'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    if fields:
        Report.objects.filter(pk=report.pk).update(**fields)
        report.refresh_from_db()
    return report


class SetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量空态分类')
        self.seller = make_user('report-empty-seller')
        self.item = Item.objects.create(
            title='举报质量空态商品', description='用于空态测试',
            price='10.00', category=self.category, condition='良好',
            seller=self.seller,
        )

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        report = build_report_resolution_quality(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['outcome']['total_count'], 0)
        # 空分母报 None 不报 0：0 会被读成「驳回率是零」，而事实是「没有已完结举报」。
        self.assertIsNone(report['outcome']['rejection_rate'])
        self.assertIsNone(report['note']['fill_rate'])
        self.assertIsNone(report['reporters']['once_only_rate'])
        self.assertTrue(report['concentration']['is_empty'])
        self.assertIn('还没有举报记录', report['summary'])

    def test_a_report_created_before_the_window_is_excluded(self):
        reporter = make_user('report-window-reporter')
        Report.objects.create(item=self.item, reporter=reporter, reason='spam')
        Report.objects.filter(pk=self.item.reports.first().pk).update(
            created_at=self.now - timedelta(days=45),
        )
        report = build_report_resolution_quality(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_report_resolution_quality(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_band_and_note_length_boundaries(self):
        """分档边界按文档给的上界走，不按感觉。"""
        self.assertEqual(_band_index(1), 0)
        self.assertEqual(_band_index(3), 1)
        self.assertEqual(_band_index(4), 2)
        self.assertEqual(_band_index(9), 2)
        self.assertEqual(_band_index(10), 3)
        self.assertEqual(_band_index(40), 3)
        self.assertEqual(_note_length_index(4), 0)
        self.assertEqual(_note_length_index(5), 1)
        self.assertEqual(_note_length_index(15), 2)
        self.assertEqual(_note_length_index(40), 3)


class OutcomeTests(TestCase):
    """总结局与右删失样本。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量结局分类')
        self.seller = make_user('report-outcome-seller')
        self.item = Item.objects.create(
            title='举报质量结局商品', description='用于结局测试',
            price='10.00', category=self.category, condition='良好',
            seller=self.seller,
        )
        self.reporter = make_user('report-outcome-reporter')

    def test_open_reports_are_right_censored_and_out_of_the_denominator(self):
        """还在流程里的举报是右删失样本，单独数规模，不混进驳回率的分母。

        举报还没判，谈不上被驳回还是被支持。把它算进分母会让驳回率凭空变低——
        一批没人处理的举报会把「处理得严不严」这个数洗淡。
        """
        for reason, status in (
            ('scam', 'resolved'), ('scam', 'rejected'),
            ('spam', 'pending'), ('spam', 'reviewing'),
        ):
            item = Item.objects.create(
                title=f'结局 {reason} {status}', description='x',
                price='10.00', category=self.category, condition='良好',
                seller=self.seller,
            )
            make_report(item, self.reporter, reason=reason, status=status)

        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertEqual(result['outcome']['total_count'], 4)
        self.assertEqual(result['outcome']['closed_count'], 2)
        self.assertEqual(result['outcome']['rejected_count'], 1)
        self.assertEqual(result['outcome']['resolved_count'], 1)
        self.assertEqual(result['outcome']['open_count'], 2)
        self.assertEqual(result['outcome']['rejection_rate'], 50.0)

    def test_only_closed_reports_have_a_rejection_rate(self):
        make_report(self.item, self.reporter, status='pending')
        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertTrue(result['outcome']['open_count'])
        self.assertIsNone(result['outcome']['rejection_rate'])
        self.assertFalse(result['outcome']['has_closed'])


class ReasonRowsTests(TestCase):
    """驳回率按 reason 拆。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量原因分类')
        self.seller = make_user('report-reason-seller')
        self.reporter = make_user('report-reason-reporter')

    def test_rejection_rate_is_reported_per_reason_not_as_a_single_total(self):
        """四种原因的举证难度完全不是一回事，平均成一个总数会指错方向。

        「诈骗」类驳回率上升可能是用户在拿举报当谈判筹码，「广告」类驳回率上升可能只是
        审核标准在变。按 reason 拆开，这是两条完全不同的线索。
        """
        for index in range(3):
            item = Item.objects.create(
                title=f'诈骗 {index}', description='x',
                price='10.00', category=self.category, condition='良好',
                seller=self.seller,
            )
            make_report(item, self.reporter, reason='scam', status='rejected')
        for index in range(3):
            item = Item.objects.create(
                title=f'广告 {index}', description='x',
                price='10.00', category=self.category, condition='良好',
                seller=self.seller,
            )
            make_report(item, self.reporter, reason='spam', status='resolved')

        result = build_report_resolution_quality(days=30, now=self.now)
        rows = {row['reason']: row for row in result['reason_rows']}
        self.assertEqual(rows['scam']['closed_count'], 3)
        self.assertEqual(rows['scam']['rejection_rate'], 100.0)
        self.assertEqual(rows['spam']['rejection_rate'], 0.0)
        # 整体驳回率仍然报——它是个总结局，只是不能单独拿它下判断。
        self.assertEqual(result['outcome']['rejection_rate'], 50.0)

    def test_every_registered_reason_appears_even_when_unused(self):
        """周期内一条都没有的举报原因也要报出来。

        漏掉它会让读者以为这一类也是齐的；报出来是「这一类没人用」，是同一个事实的
        另一面。
        """
        make_report(self.item_for('used'), self.reporter, reason='scam',
                         status='resolved')
        result = build_report_resolution_quality(days=30, now=self.now)
        reasons = {row['reason'] for row in result['reason_rows']}
        self.assertEqual(reasons, {'scam', 'spam', 'inappropriate', 'other'})
        unused = [row for row in result['reason_rows'] if row['reason'] == 'spam'][0]
        self.assertTrue(unused['is_empty'])
        self.assertIsNone(unused['rejection_rate'])

    def item_for(self, label):
        return Item.objects.create(
            title=label, description='x', price='10.00',
            category=self.category, condition='良好', seller=self.seller,
        )

    def test_a_small_sample_reason_is_flagged_not_judged(self):
        """少于门槛的 reason 只报数，不下「这一类更容易被驳回」的结论。"""
        make_report(self.item_for('small'), self.reporter, reason='scam',
                         status='rejected')
        result = build_report_resolution_quality(days=30, now=self.now)
        row = [r for r in result['reason_rows'] if r['reason'] == 'scam'][0]
        self.assertTrue(row['is_small_sample'])
        self.assertEqual(row['closed_count'], 1)

    def test_a_reason_absent_from_choices_still_gets_a_row(self):
        """库里出现了选择项之外的原因也要露出来。

        只报 `REASON_CHOICES` 就等于假设库里只有这四个值。脏数据、或者以后加了选择项
        而老数据还在，都会让这个假设不成立。
        """
        item = self.item_for('unknown-reason')
        make_report(item, self.reporter, reason='unknown_future', status='rejected')
        result = build_report_resolution_quality(days=30, now=self.now)
        row = [r for r in result['reason_rows'] if r['reason'] == 'unknown_future'][0]
        self.assertTrue(row)
        # 没有中文名，回退成英文键名，和通知类型版图同一个处理。
        self.assertEqual(row['label'], 'unknown_future')


class NoteStatsTests(TestCase):
    """审核意见只报写了没写和长度，一个字符内容都不读。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量意见分类')
        self.seller = make_user('report-note-seller')
        self.reporter = make_user('report-note-reporter')

    def item_for(self, label):
        return Item.objects.create(
            title=label, description='x', price='10.00',
            category=self.category, condition='良好', seller=self.seller,
        )

    def test_fill_rate_and_length_buckets(self):
        make_report(self.item_for('n1'), self.reporter, status='resolved',
                         review_note='已核实，商品信息与描述一致')
        make_report(self.item_for('n2'), self.reporter, status='rejected',
                         review_note='已处理')
        make_report(self.item_for('n3'), self.reporter, status='rejected',
                         review_note='')

        result = build_report_resolution_quality(days=30, now=self.now)
        note = result['note']
        self.assertEqual(note['closed_count'], 3)
        self.assertEqual(note['with_note_count'], 2)
        self.assertEqual(note['without_note_count'], 1)
        self.assertEqual(note['fill_rate'], 66.7)
        # 长度分档按去掉首尾空白之后的长度算，不按原始长度。
        self.assertEqual(sum(b['count'] for b in note['bucket_rows']), 2)

    def test_fill_rate_is_split_by_outcome(self):
        """被驳回的举报如果普遍不写意见，用户收到的是一个没有理由的拒绝。

        这是这套治理里最伤人的一种体验，单独数出来，不混进总填写率。
        """
        for index in range(2):
            make_report(self.item_for(f'r{index}'), self.reporter,
                             status='resolved', review_note='已核实')
        for index in range(3):
            make_report(self.item_for(f'j{index}'), self.reporter,
                             status='rejected', review_note='')

        result = build_report_resolution_quality(days=30, now=self.now)
        by_outcome = {row['key']: row for row in result['note']['by_outcome']}
        self.assertEqual(by_outcome['resolved']['fill_rate'], 100.0)
        self.assertEqual(by_outcome['rejected']['fill_rate'], 0.0)
        self.assertEqual(result['note']['outcome_gap_points'], -100.0)

    def test_a_whitespace_only_note_counts_as_unwritten(self):
        """只有空白的意见等于没写。

        空格、换行、制表符都不是理由。按去掉首尾空白之后的文本判空，不按原字段判空。
        """
        make_report(self.item_for('blank'), self.reporter, status='rejected',
                         review_note='   \n\t ')
        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertEqual(result['note']['with_note_count'], 0)
        self.assertEqual(result['note']['fill_rate'], 0.0)

    def test_the_outcome_gap_is_none_without_both_sides(self):
        """任一侧没有已完结举报时，差值不存在，不是 0。"""
        make_report(self.item_for('only-resolved'), self.reporter,
                         status='resolved', review_note='已核实')
        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertIsNone(result['note']['outcome_gap_points'])


class ConcentrationTests(TestCase):
    """被驳回的举报集中在哪些商品、哪些卖家身上。只报形状，不列名字。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量集中度分类')
        self.seller_a = make_user('report-conc-seller-a')
        self.seller_b = make_user('report-conc-seller-b')
        self.reporter_a = make_user('report-conc-reporter-a')
        self.reporter_b = make_user('report-conc-reporter-b')

    def item_for(self, label, seller, *, status='available'):
        return Item.objects.create(
            title=label, description='x', price='10.00',
            category=self.category, condition='良好', seller=seller,
            status=status,
        )

    def test_concentration_counts_items_sellers_and_inactive_items(self):
        hot = self.item_for('hot', self.seller_a)
        # 两拨不同的人举报同一件商品，都被驳回：UniqueConstraint 允许，因为是不同 reporter。
        make_report(hot, self.reporter_a, reason='scam', status='rejected')
        make_report(hot, self.reporter_b, reason='spam', status='rejected')
        # 一件已经下架的商品被举报后驳回。
        gone = self.item_for('gone', self.seller_b, status='sold')
        make_report(gone, self.reporter_a, reason='scam', status='rejected')
        # 一件被驳回但商品还在售。
        live = self.item_for('live', self.seller_b)
        make_report(live, self.reporter_b, reason='scam', status='rejected')

        result = build_report_resolution_quality(days=30, now=self.now)
        conc = result['concentration']
        self.assertEqual(conc['rejected_count'], 4)
        self.assertEqual(conc['item_count'], 3)
        self.assertEqual(conc['seller_count'], 2)
        self.assertEqual(conc['inactive_item_count'], 1)
        self.assertEqual(conc['repeat_item_count'], 1)
        self.assertEqual(conc['top_item_count'], 2)
        self.assertFalse(conc['is_empty'])

    def test_the_payload_carries_no_item_titles_or_usernames(self):
        """一份「被驳回举报最多的商品榜」是一份公开处刑清单，不是看板。

        只报规模与集中度。测试直接搜整份 payload，确认没有任何一件商品的名字、
        任何一个用户的用户名漏进去。
        """
        hot = self.item_for('会被搜到的商品名', self.seller_a)
        make_report(hot, self.reporter_a, reason='scam', status='rejected')
        result = build_report_resolution_quality(days=30, now=self.now)

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from walk(value)
            elif isinstance(node, str):
                yield node

        payload = list(walk(result))
        self.assertFalse(any('会被搜到的商品名' in text for text in payload))
        self.assertFalse(any('report-conc-reporter-a' in text for text in payload))

    def test_no_rejected_reports_reports_an_empty_concentration(self):
        live = self.item_for('live', self.seller_a)
        make_report(live, self.reporter_a, reason='scam', status='resolved')
        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertTrue(result['concentration']['is_empty'])
        self.assertEqual(result['concentration']['rejected_count'], 0)


class ReporterRowsTests(TestCase):
    """举报的人是不是同一批人。只报形状，不排名。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量举报人分类')
        self.seller = make_user('report-reporter-seller')
        self.reporter = make_user('report-reporter-user')

    def item_for(self, label):
        return Item.objects.create(
            title=label, description='x', price='10.00',
            category=self.category, condition='良好', seller=self.seller,
        )

    def test_distribution_shape_tells_emergency_from_habit(self):
        """绝大多数人只举报过一次，说明这条通道是应急性质的。

        有一批人反复在用，说明它是某些用户的常规手段——两种形状对应完全不同的治理
        动作。报的是形状，不是「谁最爱举报」的排行榜。
        """
        # 三个人各举报一次。
        for index in range(3):
            reporter = make_user(f'once-{index}')
            make_report(self.item_for(f'once-{index}'), reporter, reason='scam')
        # 一个人举报五次，每件商品只能举报一次，所以是五件不同商品。
        heavy = make_user('heavy')
        for index in range(5):
            make_report(self.item_for(f'heavy-{index}'), heavy, reason='spam')

        result = build_report_resolution_quality(days=30, now=self.now)
        reporters = result['reporters']
        self.assertEqual(reporters['reporter_count'], 4)
        self.assertEqual(reporters['report_count'], 8)
        self.assertEqual(reporters['once_only_count'], 3)
        self.assertEqual(reporters['once_only_rate'], 75.0)
        self.assertEqual(reporters['max_count'], 5)
        bands = {row['label']: row['count'] for row in reporters['band_rows']}
        self.assertEqual(bands['只举报过 1 次'], 3)
        self.assertEqual(bands['2-3 次'], 0)
        self.assertEqual(bands['4-9 次'], 1)

    def test_the_same_user_reporting_the_same_item_twice_is_not_reported(self):
        """`UniqueConstraint` 让「同一用户对同一商品重复举报」恒为 0，不报。

        报一个永远为 0 的常数，会让读者以为这里真的有一个被度量过的现象。
        同一用户对不同商品反复举报才是这条通道的真实形状，那是另一张表。
        """
        for index in range(3):
            reporter = make_user(f'dup-{index}')
            item = self.item_for(f'dup-{index}')
            make_report(item, reporter, reason='scam')
            make_report(item, reporter, reason='spam') if False else None

        result = build_report_resolution_quality(days=30, now=self.now)
        # payload 里不存在任何「重复举报同一商品」的键。
        self.assertNotIn('duplicate_item_reports', result)
        self.assertNotIn('same_item_repeat', result)
        self.assertEqual(result['reporters']['reporter_count'], 3)

    def test_an_empty_period_reports_none_not_zero(self):
        result = build_report_resolution_quality(days=30, now=self.now)
        self.assertTrue(result['reporters']['is_empty'])
        self.assertIsNone(result['reporters']['once_only_rate'])
        self.assertIsNone(result['reporters']['avg_count'])


class BoundaryTests(TestCase):
    """本模块只报告，不改写入路径。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='举报质量边界分类')
        self.seller = make_user('report-boundary-seller')
        self.reporter = make_user('report-boundary-reporter')

    def test_review_note_is_written_by_the_review_path_not_by_this_module(self):
        """`views.py:1608` 的写入路径必须原样保留。

        本模块只读不改。`review_note` 没有设为必填是一个产品决定，本模块只报「实际有多少
        人填了」，不把填写率变成强制——若有人为了「让数据好看」动了写入路径，这条测试会响。
        """
        from .forms import ReportReviewForm

        item = Item.objects.create(
            title='边界测试商品', description='x', price='10.00',
            category=self.category, condition='良好', seller=self.seller,
        )
        report = Report.objects.create(item=item, reporter=self.reporter, reason='scam')

        form = ReportReviewForm(data={'status': 'resolved', 'review_note': ''})
        self.assertTrue(form.is_valid())
        # 表单允许空意见：必填与否是产品决定，不是本模块的度量对象。
        self.assertEqual(form.cleaned_data['review_note'], '')

        report.review_note = form.cleaned_data['review_note']
        report.status = form.cleaned_data['status']
        report.save(update_fields=['status', 'review_note'])
        report.refresh_from_db()
        self.assertEqual(report.review_note, '')

    def test_the_module_does_not_read_the_detail_field(self):
        """举报人的补充说明是自由文本，可能写着电话、微信、楼栋号。

        `PERIOD_FIELDS` 里没有 `detail`——测试确认这一点，因为「忘了排除」比「故意排除」
        更难被发现。
        """
        from .report_resolution_quality import PERIOD_FIELDS
        self.assertNotIn('detail', PERIOD_FIELDS)
        # review_note 要读（只读长度与空否），所以它必须在。
        self.assertIn('review_note', PERIOD_FIELDS)


class DashboardWiringTests(TestCase):
    """面板与导出：这条治理队列的质量要出现在运营眼前。"""

    def setUp(self):
        self.now = local_tonight()
        self.staff = User.objects.create_superuser(
            username='report-quality-staff', email='staff@example.com',
            password='safe-password-123',
        )
        self.category = Category.objects.create(name='举报质量面板分类')
        self.seller = make_user('report-panel-seller')
        self.reporter = make_user('report-panel-reporter')

    def item_for(self, label):
        return Item.objects.create(
            title=label, description='x', price='10.00',
            category=self.category, condition='良好', seller=self.seller,
        )

    def test_the_dashboard_panel_renders_the_quality(self):
        self.client.force_login(self.staff)
        make_report(self.item_for('panel-a'), self.reporter, reason='scam',
                         status='rejected', review_note='已核实')

        response = self.client.get('/listings/operations/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('report_resolution_quality', response.context)
        content = response.content.decode('utf-8')
        self.assertIn('商品举报处理得公不公平', content)

    def test_the_csv_export_carries_the_same_section(self):
        self.client.force_login(self.staff)
        make_report(self.item_for('export-a'), self.reporter, reason='scam',
                         status='rejected', review_note='已核实')

        response = self.client.get('/listings/operations/export/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('商品举报处理得公不公平', content)
        self.assertIn('驳回率按举报原因拆', content)
