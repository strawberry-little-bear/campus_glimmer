# -*- coding: utf-8 -*-
"""为「交付预约异常处理得公不公平」钉住口径。

`MeetingIncident` 是站内唯一一条直指线下人身安全的申诉，而它此前被读到的程度比
`Report` 还浅：`governance_sla` 报了时效，`analytics` 报了新增计数，
`meeting_attendance` 只对照 `reason == 'no_show'` 一类、列的是具体工单。至于这条队列
**整体的处理倾向**，一处都没有被读过。`resolution_note` 这个字段的全部读者只有
`forms.py` 的表单字段和 `views.py:1727` 的写入路径。

本模块把它读出来，只报告不改写。这些测试要钉住的是：

1. 驳回率按 `reason` 拆开，不按总数报——四种类型的举证难度完全不是一回事；
2. 已完结的异常才算进驳回率，`open` / `reviewing` 是右删失样本，单独数出规模；
3. 处理意见只报「写了没写」和长度，一个字符内容都不读；按结局拆填写率；
4. 提交方向按 `Order` 的 buyer / seller 推出来，取不到订单一侧身份时归「未知」，不猜；
5. 提交人只报分布形状，不排名、不报任何人的用户名；
6. 不把驳回率和处理人挂钩——不报 `reviewer` 的任何拆分；
7. 不读 `detail` 的自由文本。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .meeting_incident_quality import (
    MIN_BUCKET_SAMPLE, NOISE_POINTS, PERIOD_FIELDS,
    _band_index, _direction_of, _note_length_index,
    build_meeting_incident_quality,
)
from .models import (
    Category, Item, MeetingAppointment, MeetingIncident, Order,
)


def local_tonight():
    """今天 23:59，一个确保晚于测试运行时刻的参照点。

    异常的 ``created_at`` 是 ``auto_now_add``，写的是真实当前时间。参照点必须晚于它，
    否则周期内的异常会被 ``created_at__lte=now`` 整批排除——用「今天中午」在下午跑测试
    就会踩到这个坑，而它长得像一个计算错误。
    """
    return timezone.localtime().replace(hour=23, minute=59, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_order(seller, buyer, label, category):
    """一笔已完成的订单，连同它的商品。

    ``MeetingAppointment`` 是 ``OneToOneField(order)``，一条异常要一场自己的安排，
    一场安排要一笔自己的订单——所以每造一条异常就新起一件商品。
    """
    item = Item.objects.create(
        title=label, description='用于交付预约异常处理质量测试',
        price='10.00', category=category, condition='良好', seller=seller,
    )
    return Order.objects.create(
        item=item, buyer=buyer, seller=seller,
        agreed_price='10.00', status='completed',
    )


def make_incident(order, reporter, accused, **kwargs):
    """建一条异常，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update
    写回。否则所有异常都落在「现在」，统计窗口读不出来。
    """
    now = kwargs.pop('now', None) or timezone.now()
    reason = kwargs.pop('reason', 'no_show')
    status = kwargs.pop('status', 'open')
    note = kwargs.pop('resolution_note', '')
    created_at = kwargs.pop('created_at', None)
    appointment = MeetingAppointment.objects.create(
        order=order, proposed_by=reporter, location=None,
        start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1), status='confirmed',
    )
    incident = MeetingIncident.objects.create(
        appointment=appointment, reported_by=reporter, accused=accused,
        reason=reason, detail='对方未到场，我在约定地点等了四十分钟。',
        status=status, resolution_note=note,
    )
    fields = {}
    if created_at is not None:
        fields['created_at'] = created_at
    if status in ('resolved', 'dismissed'):
        fields['reviewed_at'] = created_at or now
    if fields:
        MeetingIncident.objects.filter(pk=incident.pk).update(**fields)
        incident.refresh_from_db()
    return incident


class SetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量空态分类')
        self.seller = make_user('incident-empty-seller')
        self.buyer = make_user('incident-empty-buyer')
        self.order = make_order(self.seller, self.buyer, '空态商品', self.category)

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertFalse(result['has_data'])
        self.assertEqual(result['outcome']['total_count'], 0)
        # 空分母报 None 不报 0：0 会被读成「驳回率是零」，而事实是「没有已完结异常」。
        self.assertIsNone(result['outcome']['rejection_rate'])
        self.assertIsNone(result['note']['fill_rate'])
        self.assertIsNone(result['reporters']['once_only_rate'])
        self.assertIsNone(result['direction']['gap_points'])
        self.assertTrue(result['note']['is_empty'])
        self.assertTrue(result['direction']['is_empty'])
        self.assertTrue(result['reporters']['is_empty'])
        self.assertIn('还没有交付预约异常', result['summary'])

    def test_an_incident_created_before_the_window_is_excluded(self):
        make_incident(self.order, self.buyer, self.seller,
                      created_at=self.now - timedelta(days=45))
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertFalse(result['has_data'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        result = build_meeting_incident_quality(days=999, now=self.now)
        self.assertEqual(result['period_days'], 30)

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

    def test_direction_is_derived_from_the_order_not_from_a_guess(self):
        # 提交人既不是买家也不是卖家时不猜方向。猜一个方向比报「未知」更糟。
        self.assertEqual(_direction_of(self.buyer.id, self.buyer.id, self.seller.id),
                         'buyer_reports_seller')
        self.assertEqual(_direction_of(self.seller.id, self.buyer.id, self.seller.id),
                         'seller_reports_buyer')
        stranger = make_user('incident-setup-stranger')
        self.assertEqual(
            _direction_of(stranger.id, self.buyer.id, self.seller.id), 'unknown',
        )
        self.assertEqual(_direction_of(None, self.buyer.id, self.seller.id), 'unknown')


class OutcomeTests(TestCase):
    """总结局与右删失样本。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量结局分类')
        self.seller = make_user('incident-outcome-seller')
        self.buyer = make_user('incident-outcome-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_open_incidents_are_right_censored_and_out_of_the_denominator(self):
        """还在流程里的异常是右删失样本，单独数规模，不混进驳回率的分母。

        异常还没判，谈不上被确认还是被驳回。把它算进分母会让驳回率凭空变低——
        一批没人处理的异常会把「处理得严不严」这个数洗淡。
        """
        make_incident(self.order_for('结局一'), self.buyer, self.seller, status='resolved')
        make_incident(self.order_for('结局二'), self.buyer, self.seller, status='dismissed')
        make_incident(self.order_for('结局三'), self.buyer, self.seller, status='open')
        make_incident(self.order_for('结局四'), self.buyer, self.seller, status='reviewing')

        result = build_meeting_incident_quality(days=30, now=self.now)
        outcome = result['outcome']
        self.assertEqual(outcome['total_count'], 4)
        self.assertEqual(outcome['closed_count'], 2)
        self.assertEqual(outcome['rejected_count'], 1)
        self.assertEqual(outcome['resolved_count'], 1)
        self.assertEqual(outcome['open_count'], 2)
        self.assertEqual(outcome['rejection_rate'], 50.0)

    def test_only_closed_incidents_have_a_rejection_rate(self):
        make_incident(self.order_for('只开不闭'), self.buyer, self.seller,
                      status='reviewing')
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertEqual(result['outcome']['open_count'], 1)
        self.assertIsNone(result['outcome']['rejection_rate'])
        self.assertFalse(result['outcome']['has_closed'])


class ReasonRowsTests(TestCase):
    """驳回率按异常类型拆。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量类型分类')
        self.seller = make_user('incident-reason-seller')
        self.buyer = make_user('incident-reason-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_rejection_rate_is_reported_per_reason_not_as_a_single_total(self):
        """四种异常类型的举证难度完全不是一回事，平均成一个总数会指错方向。

        「对方未到场」有系统里的到场登记可以对照，是四种里唯一有客观证据的；
        「现场安全问题」几乎不可能有系统证据，全凭提交人陈述。按 reason 拆开，
        这是两条完全不同的线索。
        """
        for index in range(3):
            make_incident(self.order_for(f'未到场 {index}'), self.buyer, self.seller,
                          reason='no_show', status='dismissed')
        for index in range(3):
            make_incident(self.order_for(f'迟到 {index}'), self.buyer, self.seller,
                          reason='late', status='resolved')

        result = build_meeting_incident_quality(days=30, now=self.now)
        rows = {row['reason']: row for row in result['reason_rows']}
        self.assertEqual(rows['no_show']['closed_count'], 3)
        self.assertEqual(rows['no_show']['rejection_rate'], 100.0)
        self.assertEqual(rows['late']['rejection_rate'], 0.0)
        # 整体驳回率仍然报——它是个总结局，只是不能单独拿它下判断。
        self.assertEqual(result['outcome']['rejection_rate'], 50.0)

    def test_every_registered_reason_appears_even_when_unused(self):
        """周期内一条都没有的异常类型也要报出来。

        漏掉它会让读者以为这一类也是齐的；报出来是「这一类没人用」，是同一个事实的
        另一面。
        """
        make_incident(self.order_for('用过'), self.buyer, self.seller,
                      reason='safety', status='resolved')
        result = build_meeting_incident_quality(days=30, now=self.now)
        reasons = {row['reason'] for row in result['reason_rows']}
        self.assertEqual(reasons, {'no_show', 'late', 'safety', 'other'})
        unused = [row for row in result['reason_rows'] if row['reason'] == 'other'][0]
        self.assertTrue(unused['is_empty'])
        self.assertIsNone(unused['rejection_rate'])

    def test_a_small_sample_reason_is_flagged_not_judged(self):
        """少于门槛的异常类型只报数，不下「这一类更容易被驳回」的结论。"""
        make_incident(self.order_for('小样本'), self.buyer, self.seller,
                      reason='safety', status='dismissed')
        result = build_meeting_incident_quality(days=30, now=self.now)
        row = [r for r in result['reason_rows'] if r['reason'] == 'safety'][0]
        self.assertTrue(row['is_small_sample'])
        self.assertEqual(row['closed_count'], 1)

    def test_a_reason_absent_from_choices_still_gets_a_row(self):
        """库里出现了选择项之外的异常类型也要露出来。

        只报 `REASON_CHOICES` 就等于假设库里只有这四个值。脏数据、或者以后加了选择项
        而老数据还在，都会让这个假设不成立。
        """
        make_incident(self.order_for('未知类型'), self.buyer, self.seller,
                      reason='unknown_future', status='dismissed')
        result = build_meeting_incident_quality(days=30, now=self.now)
        row = [r for r in result['reason_rows']
               if r['reason'] == 'unknown_future'][0]
        self.assertTrue(row)
        # 没有中文名，回退成英文键名，和举报处理质量同一个处理。
        self.assertEqual(row['label'], 'unknown_future')


class NoteStatsTests(TestCase):
    """处理意见只报写了没写和长度，一个字符内容都不读。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量意见分类')
        self.seller = make_user('incident-note-seller')
        self.buyer = make_user('incident-note-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_fill_rate_and_length_buckets(self):
        make_incident(self.order_for('意见一'), self.buyer, self.seller,
                      status='resolved', resolution_note='已核实，双方到场登记一致')
        make_incident(self.order_for('意见二'), self.buyer, self.seller,
                      status='dismissed', resolution_note='已处理')
        make_incident(self.order_for('意见三'), self.buyer, self.seller,
                      status='dismissed', resolution_note='')

        result = build_meeting_incident_quality(days=30, now=self.now)
        note = result['note']
        self.assertEqual(note['closed_count'], 3)
        self.assertEqual(note['with_note_count'], 2)
        self.assertEqual(note['without_note_count'], 1)
        self.assertEqual(note['fill_rate'], 66.7)
        # 长度分档按去掉首尾空白之后的长度算，不按原始长度。
        self.assertEqual(sum(b['count'] for b in note['bucket_rows']), 2)

    def test_fill_rate_is_split_by_outcome(self):
        """被驳回的异常如果普遍不写意见，提交人收到的是一个没有理由的驳回。

        这一条比举报更重：异常的另一半是指控对方，一条没有理由的驳回，会让提交人
        觉得自己被反过来罚了一次。单独数出来，不混进总填写率。
        """
        for index in range(2):
            make_incident(self.order_for(f'确认 {index}'), self.buyer, self.seller,
                          status='resolved', resolution_note='已核实到场登记')
        for index in range(3):
            make_incident(self.order_for(f'驳回 {index}'), self.buyer, self.seller,
                          status='dismissed', resolution_note='')

        result = build_meeting_incident_quality(days=30, now=self.now)
        by_outcome = {row['key']: row for row in result['note']['by_outcome']}
        self.assertEqual(by_outcome['resolved']['fill_rate'], 100.0)
        self.assertEqual(by_outcome['dismissed']['fill_rate'], 0.0)
        self.assertEqual(result['note']['outcome_gap_points'], -100.0)
        self.assertEqual(by_outcome['resolved']['average_length'], 7.0)

    def test_a_whitespace_only_note_counts_as_unwritten(self):
        """只有空白的意见等于没写。

        空格、换行、制表符都不是理由。按去掉首尾空白之后的文本判空，不按原字段判空。
        """
        make_incident(self.order_for('空白意见'), self.buyer, self.seller,
                      status='dismissed', resolution_note='   \n\t ')
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertEqual(result['note']['with_note_count'], 0)
        self.assertEqual(result['note']['fill_rate'], 0.0)

    def test_the_outcome_gap_is_none_without_both_sides(self):
        """任一侧没有已完结异常时，差值不存在，不是 0。"""
        make_incident(self.order_for('只有确认'), self.buyer, self.seller,
                      status='resolved', resolution_note='已核实')
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertIsNone(result['note']['outcome_gap_points'])
        self.assertFalse(result['note']['outcome_comparable'])

    def test_a_long_note_is_not_read_as_harder_to_appeal(self):
        """不报「长度和驳回率的关系」——观测数据里分不开，只报形状，不给方向。"""
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertNotIn('length_rejection_relation', result)
        self.assertNotIn('appeal_rate', result)


class DirectionTests(TestCase):
    """是谁在报谁。只报数字，不判「哪一方更常被驳」。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量方向分类')
        self.seller = make_user('incident-direction-seller')
        self.buyer = make_user('incident-direction-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_direction_is_derived_from_the_order_not_from_a_guess(self):
        make_incident(self.order_for('买家报卖家'), self.buyer, self.seller,
                      status='dismissed')
        make_incident(self.order_for('卖家报买家'), self.seller, self.buyer,
                      status='resolved')

        result = build_meeting_incident_quality(days=30, now=self.now)
        rows = {row['key']: row for row in result['direction']['rows']}
        self.assertEqual(rows['buyer_reports_seller']['total_count'], 1)
        self.assertEqual(rows['buyer_reports_seller']['rejection_rate'], 100.0)
        self.assertEqual(rows['seller_reports_buyer']['total_count'], 1)
        self.assertEqual(rows['seller_reports_buyer']['rejection_rate'], 0.0)
        # 两侧都只有 1 条已完结：差值不作结论。
        self.assertFalse(result['direction']['comparable'])
        self.assertIsNone(result['direction']['gap_points'])

    def test_a_reporter_outside_the_order_falls_into_unknown(self):
        """取不到订单一侧身份时不猜，单独归到「未知」。

        一条异常的提交人可能已经注销，而 Order 的 buyer / seller 是 SET_NULL。
        猜一个方向比报「未知」更糟——「未知」至少是诚实的。
        """
        stranger = make_user('incident-direction-stranger')
        make_incident(self.order_for('第三方提交'), stranger, self.seller,
                      status='dismissed')

        result = build_meeting_incident_quality(days=30, now=self.now)
        rows = {row['key']: row for row in result['direction']['rows']}
        self.assertEqual(rows['unknown']['total_count'], 1)
        self.assertEqual(rows['buyer_reports_seller']['total_count'], 0)
        self.assertFalse(rows['unknown']['is_empty'])

    def test_both_sides_over_the_threshold_are_comparable(self):
        """两侧都过样本门槛才允许把这个差值读成一个信号。"""
        for index in range(12):
            make_incident(self.order_for(f'买家侧 {index}'), self.buyer,
                          self.seller, status='dismissed')
        for index in range(12):
            make_incident(self.order_for(f'卖家侧 {index}'), self.seller,
                          self.buyer, status='resolved')

        result = build_meeting_incident_quality(days=30, now=self.now)
        direction = result['direction']
        self.assertTrue(direction['comparable'])
        # 100% 对 0%，差 100 个百分点。两侧的总量必须同时报出来。
        self.assertEqual(direction['gap_points'], 100.0)
        self.assertEqual(direction['rows'][0]['total_count'], 12)
        self.assertEqual(direction['rows'][1]['total_count'], 12)

    def test_the_payload_never_names_a_user(self):
        make_incident(self.order_for('方向匿名'), self.buyer, self.seller,
                      status='dismissed')
        result = build_meeting_incident_quality(days=30, now=self.now)

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
        self.assertFalse(any('incident-direction-buyer' in text for text in payload))
        self.assertFalse(any('incident-direction-seller' in text for text in payload))


class ReporterRowsTests(TestCase):
    """报异常的人是不是同一批人。只报形状，不排名。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量提交人分类')
        self.seller = make_user('incident-reporter-seller')
        self.buyer = make_user('incident-reporter-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_distribution_shape_tells_emergency_from_habit(self):
        """绝大多数人只报过一次，说明这条通道是应急性质的。

        有一批人反复在用，说明它是某些用户的常规手段——两种形状对应完全不同的治理
        动作。报的是形状，不是「谁最爱报异常」的排行榜。
        """
        for index in range(3):
            reporter = make_user(f'incident-once-{index}')
            make_incident(self.order_for(f'只报一次 {index}'), reporter,
                          self.seller, reason='other')
        heavy = make_user('incident-heavy')
        for index in range(5):
            make_incident(self.order_for(f'反复报 {index}'), heavy,
                          self.seller, reason='other')

        result = build_meeting_incident_quality(days=30, now=self.now)
        reporters = result['reporters']
        self.assertEqual(reporters['reporter_count'], 4)
        self.assertEqual(reporters['report_count'], 8)
        self.assertEqual(reporters['once_only_count'], 3)
        self.assertEqual(reporters['once_only_rate'], 75.0)
        self.assertEqual(reporters['max_count'], 5)
        bands = {row['label']: row['count'] for row in reporters['band_rows']}
        self.assertEqual(bands['只报过 1 次'], 3)
        self.assertEqual(bands['2-3 次'], 0)
        self.assertEqual(bands['4-9 次'], 1)

    def test_the_same_appointment_reporting_twice_is_not_reported(self):
        """`OneToOneField(appointment)` 让「同一场约会里双方各报一次」恒为 0。

        报一个永远为 0 的常数，会让读者以为这里真的有一个被度量过的现象。
        同一用户对多笔不同订单反复报异常才是这条通道的真实形状，那是另一张表。
        """
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertNotIn('same_appointment_repeat', result)
        self.assertNotIn('both_sides_reported', result)
        self.assertNotIn('reviewer_rows', result)

    def test_an_empty_period_reports_none_not_zero(self):
        result = build_meeting_incident_quality(days=30, now=self.now)
        self.assertTrue(result['reporters']['is_empty'])
        self.assertIsNone(result['reporters']['once_only_rate'])
        self.assertIsNone(result['reporters']['avg_count'])


class BoundaryTests(TestCase):
    """本模块只报告，不改写入路径。"""

    def setUp(self):
        self.now = local_tonight()
        self.category = Category.objects.create(name='异常质量边界分类')
        self.seller = make_user('incident-boundary-seller')
        self.buyer = make_user('incident-boundary-buyer')
        self.order = make_order(self.seller, self.buyer, '边界商品', self.category)

    def test_resolution_note_is_written_by_the_review_path_not_by_this_module(self):
        """`views.py:1727` 的写入路径必须原样保留。

        本模块只读不改。`resolution_note` 没有设为必填是一个产品决定，本模块只报
        「实际有多少人填了」，不把填写率变成强制——若有人为了「让数据好看」动了写入
        路径，这条测试会响。
        """
        from .forms import MeetingIncidentReviewForm

        incident = make_incident(self.order, self.buyer, self.seller)
        form = MeetingIncidentReviewForm(data={'status': 'dismissed',
                                               'resolution_note': ''})
        self.assertTrue(form.is_valid())
        # 表单允许空意见：必填与否是产品决定，不是本模块的度量对象。
        self.assertEqual(form.cleaned_data['resolution_note'], '')

        incident.status = form.cleaned_data['status']
        incident.resolution_note = form.cleaned_data['resolution_note']
        incident.save(update_fields=['status', 'resolution_note'])
        incident.refresh_from_db()
        self.assertEqual(incident.resolution_note, '')

    def test_the_module_does_not_read_the_detail_field(self):
        """提交人的情况说明是自由文本，可能写着电话、微信、楼栋号。

        `PERIOD_FIELDS` 里没有 `detail`——测试确认这一点，因为「忘了排除」比
        「故意排除」更难被发现。
        """
        self.assertNotIn('detail', PERIOD_FIELDS)
        # resolution_note 要读（只读长度与空否），所以它必须在。
        self.assertIn('resolution_note', PERIOD_FIELDS)
        # 处理人也不拆：按 reviewer 拆驳回率是一份考官成绩单。
        self.assertNotIn('reviewer_id', PERIOD_FIELDS)


class DashboardWiringTests(TestCase):
    """面板与导出：这条治理队列的质量要出现在运营眼前。"""

    def setUp(self):
        self.now = local_tonight()
        self.staff = User.objects.create_superuser(
            username='incident-quality-staff', email='staff@example.com',
            password='safe-password-123',
        )
        self.category = Category.objects.create(name='异常质量面板分类')
        self.seller = make_user('incident-panel-seller')
        self.buyer = make_user('incident-panel-buyer')

    def order_for(self, label):
        return make_order(self.seller, self.buyer, label, self.category)

    def test_the_dashboard_panel_renders_the_quality(self):
        self.client.force_login(self.staff)
        make_incident(self.order_for('面板一'), self.buyer, self.seller,
                      status='dismissed', resolution_note='已核实')

        response = self.client.get('/listings/operations/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('meeting_incident_quality', response.context)
        content = response.content.decode('utf-8')
        self.assertIn('交付预约异常处理得公不公平', content)

    def test_the_csv_export_carries_the_same_section(self):
        self.client.force_login(self.staff)
        make_incident(self.order_for('导出一'), self.buyer, self.seller,
                      status='dismissed', resolution_note='已核实')

        response = self.client.get('/listings/operations/export/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('交付预约异常处理得公不公平', content)
        self.assertIn('驳回率按异常类型拆', content)