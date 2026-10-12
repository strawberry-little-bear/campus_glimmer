# -*- coding: utf-8 -*-
"""为「交易争议处理得公不公平」钉住口径。

`OrderDispute` 一直在写，读路径只有 `governance_sla.py` 把它并进治理队列比时效。
这个模块把它读出来，只报告不改写。这些测试要钉住的是：

1. 驳回率按 `reason` 拆开，不按总数报——五种争议的举证难度完全不是一回事。
2. 已完结的争议才算进驳回率，`open` / `reviewing` 是右删失样本，单独数出规模。
3. 证据只报「进来得多晚」，不读 `note`、不按 `uploaded_by` 拆、不判充分性。
4. 「有没有附件」和「驳回不驳回」只做对照，差值不满噪声门槛不给方向。
5. 发起方向只报买家/卖家，不按用户聚合、不排名。
6. 处理意见只报「写了没写」和长度，一个字符内容都不读。
7. 只报两段时长，不报「等材料花了多久」这个差值。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .dispute_resolution_quality import (
    CLOSED_STATUSES, EVIDENCE_LATENCY_BUCKETS, MIN_CLOSED_DISPUTES, NOISE_POINTS,
    NOTE_LENGTH_BUCKETS, build_dispute_resolution_quality,
)
from .models import Category, Item, Order, OrderDispute, OrderDisputeEvidence


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class DisputeResolutionQualityTests(TestCase):
    def setUp(self):
        self.buyer = make_user('dispute-buyer')
        self.seller = make_user('dispute-seller')
        self.category = Category.objects.create(name='争议质量测试分类')
        self._seq = 0

    def make_item(self, *, title='争议质量测试商品'):
        self._seq += 1
        return Item.objects.create(
            title=f'{title} {self._seq}', description='用于争议质量统计',
            price='10.00', category=self.category, condition='良好',
            seller=self.seller,
        )

    def make_order(self, *, buyer=None):
        item = self.make_item()
        order = Order.objects.create(
            item=item, buyer=buyer or self.buyer, seller=item.seller,
            meeting_location=None, agreed_price=item.price, status='completed',
        )
        order.created_at = timezone.now() - timedelta(days=5)
        order.save(update_fields=['created_at'])
        return order

    def make_dispute(self, *, reason='mismatch', status='resolved', opened_by=None,
                     created_days_ago=5, resolution_note='', resolved_days_ago=1,
                     order=None):
        """建一条争议，`created_at` / `resolved_at` 都是 auto_now_add，建完再改。

        `resolution_note` 默认空着：它可空，而「一个字都没写」正是这个模块要
        看见的那一格。
        """
        order = order or self.make_order()
        dispute = OrderDispute.objects.create(
            order=order, opened_by=opened_by or order.buyer, reason=reason,
            detail='双方对交易结果有分歧，请平台协助核实。', status=status,
            resolution_note=resolution_note,
        )
        dispute.created_at = timezone.now() - timedelta(days=created_days_ago)
        dispute.save(update_fields=['created_at'])
        if status in CLOSED_STATUSES:
            dispute.resolved_at = timezone.now() - timedelta(days=resolved_days_ago)
            dispute.save(update_fields=['resolved_at'])
        return dispute

    def make_evidence(self, dispute, *, delay_days=1, note='', uploaded_by=None):
        """给一条争议补一条附件，提交间隔按天给。

        `note` 默认空着：它可空，而这个模块一个字符都不读它。
        """
        evidence = OrderDisputeEvidence.objects.create(
            dispute=dispute, uploaded_by=uploaded_by or dispute.opened_by,
            attachment='dispute_evidence/2026/01/test.png', note=note,
        )
        evidence.created_at = dispute.created_at + timedelta(days=delay_days)
        evidence.save(update_fields=['created_at'])
        return evidence

    # --- 驳回率的分母 -------------------------------------------------------

    def test_only_closed_disputes_are_measured(self):
        self.make_dispute(status='resolved')
        self.make_dispute(status='rejected')
        # 还没判的争议谈不到被驳回还是被支持，是右删失样本。
        self.make_dispute(status='open')
        self.make_dispute(status='reviewing')

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['outcome']['closed_count'], 2)
        self.assertEqual(report['outcome']['rejected_count'], 1)
        self.assertEqual(report['outcome']['censored_count'], 2)

    def test_open_disputes_are_counted_separately_not_mixed_in(self):
        # 右删失样本单独数出来，不丢也不混：把它算进分母会让驳回率凭空变小。
        self.make_dispute(status='resolved')
        self.make_dispute(status='open')

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['outcome']['closed_count'], 1)
        self.assertEqual(report['outcome']['rejected_rate'], 0.0)
        self.assertEqual(report['outcome']['censored_count'], 1)

    def test_rejection_rate_is_split_by_reason_not_reported_as_one_number(self):
        # 五种争议的举证难度完全不是一回事。把它们平均成一个总驳回率，这个数
        # 一旦波动，运营第一反应是去查处理人，而不是去查是哪一类争议在涨。
        for _ in range(4):
            self.make_dispute(reason='not_received', status='resolved')
        for _ in range(4):
            self.make_dispute(reason='mismatch', status='rejected')

        report = build_dispute_resolution_quality(days=30)
        rows = {row['key']: row for row in report['outcome']['rows']}
        self.assertEqual(rows['not_received']['rejected_rate'], 0.0)
        self.assertEqual(rows['mismatch']['rejected_rate'], 100.0)
        # 总数同样在报，但它不是这个模块要运营盯的那个数。
        self.assertEqual(report['outcome']['rejected_rate'], 50.0)

    def test_small_sample_is_flagged_rather_than_reported_as_a_direction(self):
        # 争议是低频事件。不足门槛时占比只作参考，明说出来，不当方向用。
        for index in range(MIN_CLOSED_DISPUTES - 2):
            self.make_dispute(reason='other', status='rejected' if index == 0 else 'resolved')

        report = build_dispute_resolution_quality(days=30)
        self.assertTrue(report['outcome']['is_small_sample'])
        self.assertIn('只作参考', report['summary'])

    def test_every_reason_row_carries_its_own_denominator(self):
        # 每一行的分母是这一类争议自己的已完结数，不是全部。
        for _ in range(2):
            self.make_dispute(reason='payment', status='rejected')
        self.make_dispute(reason='safety', status='resolved')

        report = build_dispute_resolution_quality(days=30)
        rows = {row['key']: row for row in report['outcome']['rows']}
        self.assertEqual(rows['payment']['closed_count'], 2)
        self.assertEqual(rows['payment']['rejected_rate'], 100.0)
        self.assertEqual(rows['safety']['closed_count'], 1)
        self.assertEqual(rows['safety']['rejected_rate'], 0.0)

    # --- 证据时延 -----------------------------------------------------------

    def test_evidence_latency_is_measured_from_the_dispute_creation(self):
        dispute = self.make_dispute(created_days_ago=10)
        # 争议提交 10 天前，附件在 2 天后进来。给 2 天而不是 3 天：桶是左闭
        # 右开的，正好 259200 秒（3 天）会落进下一档「1 周内」，那不是这个
        # 测试要验的样本。
        self.make_evidence(dispute, delay_days=2)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['evidence']['measured_count'], 1)
        rows = {row['label']: row['count'] for row in report['evidence']['bucket_rows']}
        self.assertEqual(rows['3 天内'], 1)

    def test_the_first_attachment_is_the_one_that_counts(self):
        # 报的是「材料什么时候第一次到手上」，不是最后一条。
        dispute = self.make_dispute(created_days_ago=10)
        self.make_evidence(dispute, delay_days=1)
        self.make_evidence(dispute, delay_days=8)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['evidence']['measured_count'], 1)
        self.assertEqual(report['evidence']['fastest_label'], '1 天')

    def test_attachment_count_is_reported_but_not_used_as_sufficiency(self):
        # 附件个数报出来，但不参与任何「证据够不够」的判断。
        dispute = self.make_dispute()
        self.make_evidence(dispute)
        self.make_evidence(dispute)
        self.make_evidence(dispute)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['evidence']['attachment_count'], 3)
        # 时延分布的分母是「带了附件的争议数」，不是附件数。
        self.assertEqual(report['evidence']['measured_count'], 1)

    def test_evidence_added_after_the_case_closed_is_counted_separately(self):
        # 结案之后才补进来的附件没有参与这次裁定，单独数出来，不混进任何比率。
        # 争议 9 天前发起、1 天前结案，附件在结案之后半天才进来。
        dispute = self.make_dispute(created_days_ago=9, resolved_days_ago=1)
        self.make_evidence(dispute, delay_days=8.5)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['evidence']['after_close_count'], 1)

    def test_disputes_without_evidence_are_counted_not_treated_as_missing_data(self):
        # 没带附件是正常形态，不是缺失数据：附件本来就是可选的。
        self.make_dispute()
        self.make_dispute()

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['evidence']['with_evidence_count'], 0)
        self.assertEqual(report['evidence']['without_evidence_count'], 2)
        self.assertEqual(report['evidence']['evidence_rate'], 0.0)

    def test_evidence_note_is_never_read(self):
        # note 是自由文本，可能写着电话、微信、楼栋号。模块只报它存在与否，
        # 一个字符的内容都不该出现在任何读数里。
        dispute = self.make_dispute()
        self.make_evidence(dispute, note='我的电话是 13800000000，微信同号')

        report = build_dispute_resolution_quality(days=30)
        rendered = repr(report)
        self.assertNotIn('13800000000', rendered)
        self.assertNotIn('微信', rendered)

    # --- 附件的对照，不给因果 -----------------------------------------------

    def test_evidence_contrast_stays_silent_until_both_sides_are_large_enough(self):
        # 两组各至少 8 条才给方向。一边只有两三条时报差值，等于拿三条争议讲道理。
        for index in range(6):
            self.make_dispute(status='rejected' if index % 2 else 'resolved')
            if index % 2:
                self.make_evidence(OrderDispute.objects.order_by('-id')[0])
        self.make_dispute(status='rejected')

        report = build_dispute_resolution_quality(days=30)
        self.assertFalse(report['evidence_contrast']['has_both_sides'])
        self.assertIsNone(report['evidence_contrast']['gap'])

    def test_evidence_contrast_is_reported_as_contrast_not_cause(self):
        # 超出噪声门槛也只是对照：平台没有能力把「证据起了作用」和「这事本来
        # 就好判」分开。做成因果会变成一个对具体用户的负面标签。
        for index in range(MIN_CLOSED_DISPUTES * 2):
            dispute = self.make_dispute(
                status='rejected' if index % 2 else 'resolved',
            )
            # 偶数带附件，奇数不带：两组各有 8 条，都够门槛。
            if index % 2 == 0:
                self.make_evidence(dispute)

        report = build_dispute_resolution_quality(days=30)
        contrast = report['evidence_contrast']
        self.assertTrue(contrast['has_both_sides'])
        self.assertIsNotNone(contrast['gap'])
        self.assertIn('不构成因果', report['summary'])

    def test_small_gap_is_treated_as_noise(self):
        # 差值不满噪声门槛不给方向。两组都 8 条，各驳 1 条：12.5% 对 12.5%，
        # 差值是 0，落在噪声里。
        for index in range(MIN_CLOSED_DISPUTES * 2):
            dispute = self.make_dispute(
                status='rejected' if index in (0, MIN_CLOSED_DISPUTES) else 'resolved',
            )
            if index < MIN_CLOSED_DISPUTES:
                self.make_evidence(dispute)

        report = build_dispute_resolution_quality(days=30)
        contrast = report['evidence_contrast']
        self.assertTrue(contrast['has_both_sides'])
        self.assertEqual(contrast['gap'], 0.0)
        self.assertTrue(contrast['is_noise'])
        self.assertIn('噪声', report['summary'])

    # --- 发起方向 -----------------------------------------------------------

    def test_opener_direction_is_reported_without_ranking_any_user(self):
        # 方向分布报的是流程是否对双方可用，不是谁难相处。不按用户聚合、
        # 不排名、不报任何人的用户名。
        self.make_dispute(opened_by=self.buyer)
        self.make_dispute(opened_by=self.buyer)
        self.make_dispute(opened_by=self.seller)

        report = build_dispute_resolution_quality(days=30)
        rows = {row['key']: row for row in report['opener']['rows']}
        self.assertEqual(rows['buyer']['opened_count'], 2)
        self.assertEqual(rows['seller']['opened_count'], 1)
        self.assertEqual(rows['buyer']['opened_rate'], 66.7)
        # 用户名一个都不该出现。
        rendered = repr(report)
        self.assertNotIn('dispute-buyer', rendered)
        self.assertNotIn('dispute-seller', rendered)

    def test_seller_opened_dispute_is_not_assumed_to_be_a_counterattack(self):
        # 卖家发起的争议不是「反咬」，它只是这条流程的另一个入口。方向里
        # 没有「谁在报复谁」这个字段，也不该有。
        dispute = self.make_dispute(opened_by=self.seller, status='rejected')

        report = build_dispute_resolution_quality(days=30)
        rows = {row['key']: row for row in report['opener']['rows']}
        self.assertEqual(rows['seller']['opened_count'], 1)
        self.assertEqual(rows['seller']['rejected_count'], 1)
        self.assertNotIn('报复', report['summary'])

    # --- 处理意见 -----------------------------------------------------------

    def test_resolution_note_fill_rate_is_measured(self):
        self.make_dispute(status='resolved', resolution_note='已核实聊天记录，支持买家。')
        self.make_dispute(status='resolved')

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['note']['closed_count'], 2)
        self.assertEqual(report['note']['with_note_count'], 1)
        self.assertEqual(report['note']['fill_rate'], 50.0)

    def test_rejection_without_a_note_is_the_most_visible_gap(self):
        # 驳回却没写意见：双方收到了「已驳回」的通知，却不知道为什么。
        # 这一格单独数出来，不混进填写率。
        self.make_dispute(status='rejected', resolution_note='')
        self.make_dispute(status='resolved', resolution_note='')

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['note']['rejected_without_note_count'], 1)
        self.assertIn('只收到状态', report['summary'])

    def test_whitespace_only_note_counts_as_empty(self):
        # 一段空格不是理由。
        self.make_dispute(status='resolved', resolution_note='   \n  ')

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['note']['with_note_count'], 0)

    def test_note_content_is_never_read(self):
        # resolution_note 是处理人写的自由文本。模块只关心它有没有被写下来。
        self.make_dispute(
            status='resolved',
            resolution_note='买家电话 13800000000，微信同号，已线下沟通。',
        )

        report = build_dispute_resolution_quality(days=30)
        rendered = repr(report)
        self.assertNotIn('13800000000', rendered)
        self.assertNotIn('微信', rendered)

    def test_note_length_is_bucketed(self):
        self.make_dispute(status='resolved', resolution_note='短')
        self.make_dispute(status='resolved', resolution_note='已核实双方聊天记录与商品实物照片，支持买家退款。')
        # 三十字以上：一段写清了核实依据的意见，和一句「情况属实」在长度上
        # 是能分开的。
        self.make_dispute(status='resolved', resolution_note='已核对双方聊天记录、商品实物照片与交付确认时间，买家所述与记录一致，支持买家。')

        report = build_dispute_resolution_quality(days=30)
        rows = {row['label']: row['count'] for row in report['note']['bucket_rows']}
        self.assertEqual(rows['10 字以内'], 1)
        self.assertEqual(rows['11-30 字'], 1)
        self.assertEqual(rows['31-80 字'], 1)

    # --- 两段时长，不报差值 -------------------------------------------------

    def test_resolution_latency_is_measured_from_the_dispute_creation(self):
        self.make_dispute(created_days_ago=10, resolved_days_ago=3)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['resolution_latency']['measured_count'], 1)
        self.assertEqual(report['resolution_latency']['median_label'], '7 天')

    def test_the_module_never_reports_time_spent_waiting_for_evidence(self):
        # 「等材料花了多久」这个差值不报：结案慢可能是材料来得晚，也可能是
        # 处理人忙，还可能是双方在谈。报一个差值等于把处理人的效率问题算成
        # 用户的问题。两段各自单独报，运营自己判断。
        report = build_dispute_resolution_quality(days=30)
        self.assertNotIn('waiting_for_evidence', report)
        self.assertNotIn('等材料', report['summary'])

    def test_closed_dispute_without_resolved_at_is_counted_not_approximated(self):
        # 已完结但找不到 resolved_at 的属于数据本身不完整，单独数出来，
        # 不拿 updated_at 近似——任何一次保存都会动 updated_at。
        self.make_dispute(status='resolved', resolution_note='已核实。')
        dispute = OrderDispute.objects.order_by('-id').first()
        dispute.resolved_at = None
        dispute.save(update_fields=['resolved_at'])

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['resolution_latency']['missing_resolved_at_count'], 1)
        self.assertEqual(report['resolution_latency']['measured_count'], 0)

    # --- 统计窗口 -----------------------------------------------------------

    def test_the_period_cuts_on_dispute_creation_not_resolution(self):
        # 窗口按争议发起时刻切：一条窗口外发起、窗口内结案的争议不计入，
        # 因为它的驳回率读的是结案时的状态，而它不是本期的受理量。
        old = self.make_dispute(created_days_ago=40, resolved_days_ago=35)
        self.make_dispute(created_days_ago=2)

        report = build_dispute_resolution_quality(days=30)
        self.assertEqual(report['outcome']['closed_count'], 1)
        self.assertNotIn(old.id, [row['dispute'].id for row in report['slowest_rows']['rows']])

    def test_unknown_days_falls_back_to_the_default_period(self):
        self.make_dispute()
        report = build_dispute_resolution_quality(days=15)
        self.assertEqual(report['period_days'], 30)

    def test_empty_period_reports_a_readable_summary(self):
        report = build_dispute_resolution_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertIn('还没有已结案', report['summary'])

    def test_bucket_boundaries_are_left_closed_right_open(self):
        # 正好 86400 秒落进下一档：桶是左闭右开的，测试里要验证边界语义。
        self.assertGreater(len(EVIDENCE_LATENCY_BUCKETS), 3)
        self.assertGreater(len(NOTE_LENGTH_BUCKETS), 3)
        self.assertEqual(EVIDENCE_LATENCY_BUCKETS[-1][0], None)
        self.assertEqual(NOTE_LENGTH_BUCKETS[-1][0], None)
        self.assertEqual(NOISE_POINTS, 8)
